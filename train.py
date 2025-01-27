from __future__ import print_function, division
import argparse
import os
from glob import glob

import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.utils.tensorboard import SummaryWriter

from model.cnn_geometric_model import CNNGeometric
from model.loss_old import TransformedGridLoss, MixedLoss
from model.loss import SplitLoss

from data.synth_dataset import SynthDataset
from data.me_dataset import MEDataset
# from data.download_datasets import download_pascal

from geotnf.transformation import SynthPairTnf

from image.normalization import NormalizeImageDict

from util.train_test_fn import train, validate_model
from util.eval_util import compute_metric
from util.torch_util import save_checkpoint, str_to_bool, BatchTensorToVars

from options.options import ArgumentParser


"""

Script to evaluate a trained model as presented in the CNNGeometric TPAMI paper
on the PF/PF-pascal/Caltech-101 and TSS datasets

"""

def load_checkpoint(checkpoint_filename, model, optimizer, device):
    if os.path.exists(checkpoint_filename):
        checkpoint = torch.load(checkpoint_filename, map_location=device)

        model.load_state_dict(checkpoint['state_dict'])
        optimizer.load_state_dict(checkpoint['optimizer'])
        epoch = checkpoint['epoch']
        best_val_loss = checkpoint['best_val_loss']
        # Deducing optimizer steps count
        last_idx = optimizer.state_dict()['param_groups'][0]['params'][0]
        last_epoch = optimizer.state_dict()['state'][last_idx]['step']
        print('Continue training from %d epoch' % epoch)
    else:
        epoch = 1
        best_val_loss = float("inf")
        last_epoch = -1

    return model, optimizer, epoch, best_val_loss, last_epoch

def main():

    args,arg_groups = ArgumentParser(mode='train').parse()
    print(args)

    use_cuda = torch.cuda.is_available()
    use_me = args.use_me
    device = torch.device('cuda') if use_cuda else torch.device('cpu')
    # Seed
    # torch.manual_seed(args.seed)
    # if use_cuda:
        # torch.cuda.manual_seed(args.seed)

    # CNN model and loss
    print('Creating CNN model...')
    if args.geometric_model == 'affine_simple':
        cnn_output_dim = 3
    elif args.geometric_model == 'affine_simple_4':
        cnn_output_dim = 4
    else:
        raise NotImplementedError('Specified geometric model is unsupported')

    model = CNNGeometric(use_cuda=use_cuda,
                         output_dim=cnn_output_dim,
                         **arg_groups['model'])

    if args.geometric_model == 'affine_simple':
        init_theta = torch.tensor([0.0, 1.0, 0.0], device = device)
    elif args.geometric_model == 'affine_simple_4':
        init_theta = torch.tensor([0.0, 1.0, 0.0, 0.0], device = device)
        
    try:
        model.FeatureRegression.linear.bias.data += init_theta
    except:
        model.FeatureRegression.resnet.fc.bias.data += init_theta

    if args.loss == 'split':
        print('Using Split loss')
        loss = SplitLoss(use_cuda=use_cuda,
                         geometric_model=args.geometric_model,
                         grid_size=20)
    elif args.loss == 'mixed':
        print('Using grid+MSE loss...')
        loss = MixedLoss(alpha=1000,
                         use_cuda=use_cuda, 
                         geometric_model=args.geometric_model)
    elif args.loss == 'mse':
        print('Using MSE loss...')
        loss = nn.MSELoss()
    elif args.loss == 'grid':
        print('Using grid loss...')
        loss = TransformedGridLoss(use_cuda=use_cuda,
                                   geometric_model=args.geometric_model)
    else:
        raise NotImplementedError('Specifyed loss %s is not supported' % args.loss)

    # Initialize Dataset objects
    if use_me:
        dataset = MEDataset(geometric_model=args.geometric_model, 
                        dataset_csv_path=args.dataset_csv_path, 
                        dataset_csv_file='train.csv', 
                        dataset_image_path=args.dataset_image_path,
                        input_height=args.input_height, input_width=args.input_width, 
                        crop=args.crop_factor, 
                        use_conf=args.use_conf, 
                        use_random_patch=args.use_random_patch,
                        normalize_inputs=args.normalize_inputs,
                        random_sample=args.random_sample)

        dataset_val = MEDataset(geometric_model=args.geometric_model, 
                        dataset_csv_path=args.dataset_csv_path, 
                        dataset_csv_file='val.csv', 
                        dataset_image_path=args.dataset_image_path,
                        input_height=args.input_height, input_width=args.input_width, 
                        crop=args.crop_factor, 
                        use_conf=args.use_conf, 
                        use_random_patch=args.use_random_patch,
                        normalize_inputs=args.normalize_inputs,
                        random_sample=args.random_sample)

    else:

        dataset = SynthDataset(geometric_model=args.geometric_model,
                        dataset_csv_path=args.dataset_csv_path,
                        dataset_csv_file='train.csv',
                        dataset_image_path=args.dataset_image_path,
                        transform=NormalizeImageDict(['image']),
                        random_sample=args.random_sample)

        dataset_val = SynthDataset(geometric_model=args.geometric_model,
                        dataset_csv_path=args.dataset_csv_path,
                        dataset_csv_file='val.csv',
                        dataset_image_path=args.dataset_image_path,
                        transform=NormalizeImageDict(['image']),
                        random_sample=args.random_sample)

    # Set Tnf pair generation func
    if use_me:
        pair_generation_tnf = BatchTensorToVars(use_cuda=use_cuda)
    elif args.geometric_model == 'affine_simple' or args.geometric_model == 'affine_simple_4':
        pair_generation_tnf = SynthPairTnf(geometric_model='affine',
				       use_cuda=use_cuda)
    else:
        raise NotImplementedError('Specified geometric model is unsupported')

    # Initialize DataLoaders
    dataloader = DataLoader(dataset, batch_size=args.batch_size,
                            shuffle=True, num_workers=4)

    dataloader_val = DataLoader(dataset_val, batch_size=args.batch_size,
                                shuffle=True, num_workers=4)

    # Optimizer
    optimizer = optim.Adam(model.FeatureRegression.parameters(), lr=args.lr)

    # Train

    # Set up names for checkpoints
    ckpt = args.trained_model_fn + '_' + args.geometric_model + '_' + args.loss + '_loss_'
    checkpoint_path = os.path.join(args.trained_model_dir,
                                    args.trained_model_fn,
                                    ckpt + '.pth.tar')
    if not os.path.exists(args.trained_model_dir):
        os.mkdir(args.trained_model_dir)

    # Set up TensorBoard writer
    if not args.log_dir:
        tb_dir = os.path.join(args.trained_model_dir, args.trained_model_fn + '_tb_logs')
    else:
        tb_dir = os.path.join(args.log_dir, args.trained_model_fn + '_tb_logs')

    logs_writer = SummaryWriter(tb_dir)
    # add graph, to do so we have to generate a dummy input to pass along with the graph
    if use_me:
        dummy_input = {
            'mv_L2R': torch.rand([args.batch_size, 2, 216, 384], device = device),
            'mv_R2L': torch.rand([args.batch_size, 2, 216, 384], device = device),
            'grid_L2R': torch.rand([args.batch_size, 2, 216, 384], device = device),
            'grid_R2L': torch.rand([args.batch_size, 2, 216, 384], device = device),
            'grid': torch.rand([args.batch_size, 2, 216, 384], device = device),
            'conf_L': torch.rand([args.batch_size, 1, 216, 384], device = device),
            'conf_R': torch.rand([args.batch_size, 1, 216, 384], device = device),
            'theta_GT': torch.rand([args.batch_size, 4], device = device),
        }

    else:
        dummy_input = {'source_image': torch.rand([args.batch_size, 3, 240, 240], device = device),
                       'target_image': torch.rand([args.batch_size, 3, 240, 240], device = device),
                       'theta_GT': torch.rand([args.batch_size, 2, 3], device = device)}

    logs_writer.add_graph(model, dummy_input)

    # Start of training
    print('Starting training...')

    best_val_loss = float("inf")

    max_batch_iters = len(dataloader)
    print('Iterations for one epoch:', max_batch_iters)
    epoch_to_change_lr = int(args.lr_max_iter / max_batch_iters * 2 + 0.5)

    # Loading checkpoint
    model, optimizer, start_epoch, best_val_loss, last_epoch = load_checkpoint(checkpoint_path, model, optimizer, device)
    
    # Scheduler
    if args.lr_scheduler == 'cosine':
        is_cosine_scheduler = True
        scheduler = torch.optim.lr_scheduler.CosineAnnealingLR(optimizer,
                                                               T_max=args.lr_max_iter,
                                                               eta_min=1e-7,
                                                               last_epoch=last_epoch)
    elif args.lr_scheduler == 'cosine_restarts':
        is_cosine_scheduler = True
        scheduler = torch.optim.lr_scheduler.CosineAnnealingWarmRestarts(optimizer, 
                                                                         T_0=args.lr_max_iter, 
                                                                         T_mult=2,
                                                                         last_epoch=last_epoch)

    elif args.lr_scheduler == 'exp':
        is_cosine_scheduler = False
        if last_epoch > 0:
            last_epoch /= max_batch_iters
        scheduler = torch.optim.lr_scheduler.ExponentialLR(optimizer,
                                                           gamma=args.lr_decay,
                                                           last_epoch=last_epoch)
    # elif args.lr_scheduler == 'step':
        # step_scheduler = torch.optim.lr_scheduler.StepLR(optimizer, 10, gamma=0.1)
        # scheduler = False
    else:
        is_cosine_scheduler = False
        scheduler = False

    for epoch in range(1, start_epoch):
        if args.lr_scheduler == 'cosine' and (epoch % epoch_to_change_lr == 0):
            scheduler.state_dict()['base_lrs'][0] *= args.lr_decay

    for epoch in range(start_epoch, args.num_epochs+1):

        # we don't need the average epoch loss so we assign it to _
        _ = train(epoch, model, loss, optimizer,
                  dataloader, pair_generation_tnf,
                  log_interval=args.log_interval,
                  scheduler=scheduler,
                  is_cosine_scheduler=is_cosine_scheduler,
                  tb_writer=logs_writer,
                  visualize_loss=args.visualize_loss)
        
        # Step non-cosine scheduler
        if scheduler and not is_cosine_scheduler:
            scheduler.step()

        val_loss = validate_model(model, loss,
                                  dataloader_val, pair_generation_tnf,
                                  epoch, logs_writer)

        # Change lr_max in cosine annealing
        if args.lr_scheduler == 'cosine' and (epoch % epoch_to_change_lr == 0):
            scheduler.state_dict()['base_lrs'][0] *= args.lr_decay

        if (epoch % epoch_to_change_lr == epoch_to_change_lr // 2) or epoch == 1:
            compute_metric('absdiff', model, args.geometric_model, None, None, dataset_val, dataloader_val, pair_generation_tnf, args.batch_size, args)

        # remember best loss
        is_best = val_loss < best_val_loss
        best_val_loss = min(val_loss, best_val_loss)
        save_checkpoint({
                         'epoch': epoch + 1,
                         'args': args,
                         'state_dict': model.state_dict(),
                         'best_val_loss': best_val_loss,
                         'optimizer': optimizer.state_dict(),
                         },
                        is_best, checkpoint_path)

    logs_writer.close()
    print('Done!')


if __name__ == '__main__':
    main()
# train.py --geometric-model affine_simple_4 --use-me True --training-dataset 3d --dataset-csv-path ./training_data/3d-random --dataset-image-path ../3d_pictures_raw --fr-channels 4 64 128 128 64 --num-epochs 1