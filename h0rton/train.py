# -*- coding: utf-8 -*-
"""Training the Bayesian neural network (BNN).
This script trains the BNN according to the config specifications.

Example
-------
To run this script, pass in the path to the user-defined training config file as the argument::
    
    $ train h0rton/example_user_config.py

"""

import os, sys
import random
import argparse
from addict import Dict
import numpy as np # linear algebra
from tqdm import tqdm
# torch modules
import torch
import torch.nn as nn
import torch.optim as optim
from torch.utils.data import DataLoader
from torch.utils.data.sampler import SubsetRandomSampler
import torchvision.models
from torch.utils.tensorboard import SummaryWriter
# h0rton modules
from h0rton.trainval_data import XYData
from h0rton.configs import BNNConfig
import h0rton.losses
from h0rton.plotting import BNNInterpreter
import h0rton.train_utils as train_utils

def parse_args():
    """Parse command-line arguments
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('user_cfg_path', help='path to the user-defined training config file')
    #parser.add_argument('--n_data', default=None, dest='n_data', type=int,
    #                    help='size of dataset to generate (overrides config file)')
    args = parser.parse_args()
    # sys.argv rerouting for setuptools entry point
    if args is None:
        args = Dict()
        args.user_cfg_path = sys.argv[0]
        #args.n_data = sys.argv[1]
    return args

def seed_everything(global_seed):
    """Seed everything for reproducibility

    global_seed : int
        seed for `np.random`, `random`, and relevant `torch` backends

    """
    np.random.seed(global_seed)
    random.seed(global_seed)
    torch.manual_seed(global_seed)
    torch.cuda.manual_seed(global_seed)
    torch.backends.cudnn.deterministic = True
    torch.backends.cudnn.benchmark = False

def main():
    args = parse_args()
    cfg = BNNConfig.from_file(args.user_cfg_path)
    seed_everything(cfg.global_seed)

    # Define training data and loader
    train_data = XYData(cfg.data.train_dir, data_cfg=cfg.data)
    train_loader = DataLoader(train_data, batch_size=cfg.optim.batch_size, shuffle=True, drop_last=True)
    n_train = train_data.n_data

    # Define val data and loader
    val_data = XYData(cfg.data.val_dir, data_cfg=cfg.data)
    val_loader = DataLoader(val_data, batch_size=cfg.optim.batch_size, shuffle=True, drop_last=True)
    n_val = val_data.n_data

    # Define plotting data (subset of val data) and loader
    if cfg.log.monitor_1d_marginal_mapping:
        plot_data_sampler = SubsetRandomSampler(np.arange(cfg.data.n_plotting))
        plot_data_loader = DataLoader(val_data, batch_size=len(cfg.data.n_plotting), sampler=plot_data_sampler)
        # Define plotter object
        plotter = BNNInterpreter(cfg.model.type, cfg.data.Y_dim, cfg.device)

    # Instantiate loss function
    loss_fn = getattr(h0rton.losses, cfg.model.likelihood_class)(Y_dim=cfg.data.Y_dim, device=cfg.device)
    # Instantiate model
    net = getattr(torchvision.models, cfg.model.architecture)(pretrained=False)
    n_filters = net.fc.in_features # number of output nodes in 2nd-to-last layer
    net.fc = nn.Linear(in_features=n_filters, out_features=loss_fn.out_dim) # replace final layer
    net.to(cfg.device)
    # Instantiate optimizer
    optimizer = optim.Adam(net.parameters(), lr=cfg.optim.learning_rate, amsgrad=True, weight_decay=cfg.optim.weight_decay)
    lr_scheduler = optim.lr_scheduler.MultiStepLR(optimizer, milestones=cfg.optim.lr_scheduler.milestones, gamma=cfg.optim.lr_scheduler.gamma)
    logger = SummaryWriter()

    if not os.path.exists(cfg.log.checkpoint_dir):
        os.mkdir(cfg.log.checkpoint_dir)

    if cfg.model.load_state:
        net, optimizer, lr_scheduler, epoch = train_utils.load_state_dict(cfg.model.state_path, net, optimizer, lr_scheduler, cfg.optim.n_epochs, cfg.device)
        epoch += 1 # resume with next epoch
    else:
        epoch = 0

    model_path = ''
    progress = tqdm(range(epoch, cfg.optim.n_epochs))
    for epoch in progress:
        net.train()
        total_loss = 0.0

        for batch_idx, (X_, Y_) in enumerate(train_loader):
            #X = Variable(torch.Tensor(X_)).to(cfg.device)
            #Y = Variable(torch.Tensor(Y_)).to(cfg.device)
            X = X_.to(cfg.device)
            Y = Y_.to(cfg.device)

            pred = net(X)
            loss = loss_fn(pred, Y)
            total_loss += loss.item()*cfg.optim.batch_size

            optimizer.zero_grad()
            loss.backward()
            optimizer.step()
        # Step every epoch
        lr_scheduler.step()

        with torch.no_grad():
            net.eval()
            total_val_loss = 0.0

            for batch_idx, (X_, Y_) in enumerate(val_loader):
                X = X_.to(cfg.device)
                Y = Y_.to(cfg.device)

                pred = net(X)
                loss = loss_fn(pred, Y)
                total_val_loss += loss.item()*cfg.optim.batch_size

            epoch_avg_train_loss = total_loss/n_train
            epoch_avg_val_loss = total_val_loss/n_val

            tqdm.write("Epoch [{}/{}]: TRAIN Loss: {:.4f}".format(epoch+1, cfg.optim.n_epochs, epoch_avg_train_loss))
            tqdm.write("Epoch [{}/{}]: VALID Loss: {:.4f}".format(epoch+1, cfg.optim.n_epochs, epoch_avg_val_loss))
            
            if (epoch + 1)%(cfg.log.logging_interval) == 0:
                # Log train and val losses
                logger.add_scalars('metrics/loss',
                                   {'train': epoch_avg_train_loss, 'val': epoch_avg_val_loss},
                                   epoch)

                # Log histograms of named parameters
                for param_name, param in net.named_parameters():
                    logger.add_histogram(param_name, param.clone().cpu().data.numpy(), epoch)

                # Get 1D marginal mapping plots
                # FIXME: operation is subset of the validation loop
                # but we want to free RAM
                if cfg.log.monitor_1d_marginal_mapping:
                    for batch_idx, (X_, Y_) in enumerate(plot_data_loader):
                        X_plt = X_.to(cfg.device)
                        Y_plt = Y_.to(cfg.device)
                        pred_plt = net(X_plt).cpu().numpy()

                    plotter.set_normal_mixture_params(pred_plt)
                    for param_idx, param_name in enumerate(cfg.data.Y_cols):
                        tag = '1d_mapping/{:s}'.format(param_name)
                        fig = plotter.get_1d_mapping_fig(param_name, param_idx, Y_plt[:, param_idx])
                        logger.add_figure(tag, fig)

            if (epoch + 1)%(cfg.log.checkpoint_interval) == 0:
                os.remove(model_path) if os.path.exists(model_path) else None
                model_path = train_utils.save_state_dict(net, optimizer, lr_scheduler, epoch_avg_train_loss, epoch_avg_val_loss, cfg.log.checkpoint_dir, cfg.model.architecture, epoch)

    logger.close()
    os.remove(model_path) if os.path.exists(model_path) else None
    model_path = train_utils.save_state_dict(net, optimizer, lr_scheduler, epoch_avg_train_loss, epoch_avg_val_loss, cfg.log.checkpoint_dir, cfg.model.architecture, epoch)
    print("Saved model at {:s}".format(os.path.abspath(model_path)))

if __name__ == '__main__':
    main()