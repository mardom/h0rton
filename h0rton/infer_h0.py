# -*- coding: utf-8 -*-
"""Training the Bayesian neural network (BNN).
This script performs H0 inference on a test (or validation) sample

Example
-------
To run this script, pass in the path to the user-defined training config file as the argument::
    
    $ infer_h0 h0rton/h0_inference_config.json

"""
import os
import sys
import argparse
import random
import time
from addict import Dict
from tqdm import tqdm
import progressbar
from ast import literal_eval
import json
import glob
import numpy as np
import pandas as pd
import scipy.stats as stats
import torch
from torch.utils.data import DataLoader
import h0rton.models
# Baobab modules
import baobab.sim_utils
from baobab import BaobabConfig
# H0rton modules
from h0rton.configs import TrainValConfig, TestConfig
import h0rton.losses
import h0rton.train_utils as train_utils
from h0rton.h0_inference import DoubleGaussianBNNPosterior, H0Posterior, plot_h0_histogram
from h0rton.trainval_data import XYCosmoData

def up():
    # My terminal breaks if we don't flush after the escape-code
    sys.stdout.write('\x1b[1A')
    sys.stdout.flush()

def down():
    # I could use '\x1b[1B' here, but newline is faster and easier
    sys.stdout.write('\n')
    sys.stdout.flush()

def parse_args():
    """Parse command-line arguments
    """
    parser = argparse.ArgumentParser()
    parser.add_argument('test_config_file_path', help='path to the user-defined test config file')
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

def get_baobab_config(baobab_out_dir):
    """Load the baobab log

    Parameters
    ----------
    baobab_out_dir : str or os.path object
        path to the baobab output directory

    Returns
    -------
    baobab.BaobabConfig object
        log of the baobab-generated dataset, including the input config

    """
    baobab_log_path = glob.glob(os.path.join(baobab_out_dir, 'log_*_baobab.json'))[0]
    with open(baobab_log_path, 'r') as f:
        log_str = f.read()
    baobab_cfg = BaobabConfig(Dict(json.loads(log_str)))
    return baobab_cfg

def main():
    args = parse_args()
    test_cfg = TestConfig.from_file(args.test_config_file_path)
    baobab_cfg = get_baobab_config(test_cfg.data.test_dir)
    train_val_cfg = TrainValConfig.from_file(test_cfg.train_val_config_file_path)
    # Set device and default data type
    device = torch.device(test_cfg.device_type)
    if device.type == 'cuda':
        torch.set_default_tensor_type('torch.cuda.FloatTensor')
    else:
        torch.set_default_tensor_type('torch.FloatTensor')
    seed_everything(test_cfg.global_seed)
    
    ############
    # Data I/O #
    ############
    test_data = XYCosmoData(test_cfg.data.test_dir, data_cfg=train_val_cfg.data)
    n_test = test_cfg.data.n_test # number of lenses in the test set
    test_loader = DataLoader(test_data, batch_size=n_test, shuffle=False, drop_last=True)
    cosmo_df = test_data.cosmo_df # cosmography observables
    # Output directory into which the H0 histograms and H0 samples will be saved
    out_dir = test_cfg.out_dir
    if not os.path.exists(out_dir):
        os.makedirs(out_dir)
        print("Destination folder path: {:s}".format(out_dir))
    else:
        raise OSError("Destination folder already exists.")

    ######################
    # Load trained state #
    ######################
    # Instantiate loss function
    loss_fn = getattr(h0rton.losses, train_val_cfg.model.likelihood_class)(Y_dim=train_val_cfg.data.Y_dim, device=device)
    # Instantiate posterior (for logging)
    bnn_post = getattr(h0rton.h0_inference.gaussian_bnn_posterior, loss_fn.posterior_name)(train_val_cfg.data.Y_dim, device, train_val_cfg.data.train_Y_mean, train_val_cfg.data.train_Y_std)
    # Instantiate model
    net = getattr(h0rton.models, train_val_cfg.model.architecture)(num_classes=loss_fn.out_dim)
    net.to(device)
    # Load trained weights from saved state
    net, epoch = train_utils.load_state_dict_test(test_cfg.state_dict_path, net, train_val_cfg.optim.n_epochs, device)
    with torch.no_grad():
        net.eval()
        for X_, Y_ in test_loader:
            X = X_.to(device)
            Y = Y_.to(device)
            pred = net(X)
            break

    # Export the input images X for later error analysis
    if test_cfg.export.images:
        for lens_i in range(n_test):
            X_img_path = os.path.join(out_dir, 'X_{0:04d}.npy'.format(lens_i))
            np.save(X_img_path, X[lens_i, 0, :, :].cpu().numpy())

    ################
    # H0 Posterior #
    ################
    h0_prior = getattr(stats, test_cfg.h0_prior.dist)(**test_cfg.h0_prior.kwargs)
    kappa_ext_prior = getattr(stats, test_cfg.kappa_ext_prior.dist)(**test_cfg.kappa_ext_prior.kwargs)
    aniso_param_prior = getattr(stats, test_cfg.aniso_param_prior.dist)(**test_cfg.aniso_param_prior.kwargs)
    # FIXME: hardcoded
    kwargs_model = dict(
                        lens_model_list=['SPEMD', 'SHEAR'],
                        lens_light_model_list=['SERSIC_ELLIPSE'],
                        source_light_model_list=['SERSIC_ELLIPSE'],
                        point_source_model_list=['SOURCE_POSITION'],
                       #'point_source_model_list' : ['LENSED_POSITION']
                       )
    h0_post = H0Posterior(
                          H0_prior=h0_prior,
                          kappa_ext_prior=kappa_ext_prior,
                          aniso_param_prior=aniso_param_prior,
                          exclude_vel_disp=test_cfg.h0_posterior.exclude_velocity_dispersion,
                          kwargs_model=kwargs_model,
                          baobab_time_delays=test_cfg.time_delay_likelihood.baobab_time_delays,
                          kinematics=baobab_cfg.bnn_omega.kinematics,
                          Om0=baobab_cfg.bnn_omega.cosmology.Om0,
                          define_src_pos_wrt_lens=train_val_cfg.data.define_src_pos_wrt_lens,
                          )
    # Get H0 samples for each system
    if not test_cfg.time_delay_likelihood.baobab_time_delays:
        if 'abcd_ordering_i' not in cosmo_df:
            raise ValueError("If the time delay measurements were not generated using Baobab, the user must specify the order of image positions in which the time delays are listed, in order of increasing dec.")
    required_params = h0_post.required_params

    ########################
    # Lens Model Posterior #
    ########################
    n_samples = test_cfg.h0_posterior.n_samples # number of h0 samples per lens

    # Sample from the BNN posterior
    sampling_buffer = 3
    bnn_post = DoubleGaussianBNNPosterior(test_data.Y_dim, device, train_val_cfg.data.train_Y_mean, train_val_cfg.data.train_Y_std)
    bnn_post.set_sliced_pred(pred)
    lens_model_samples = bnn_post.sample(sampling_buffer*n_samples, sample_seed=test_cfg.global_seed).reshape(-1, test_data.Y_dim) # [n_test*n_samples, Y_dim]
    lens_model_samples_df = pd.DataFrame(lens_model_samples, columns=train_val_cfg.data.Y_cols)
    lens_model_samples_values = lens_model_samples_df[required_params].values.reshape(n_test, sampling_buffer*n_samples, -1)

    Y_orig = bnn_post.transform_back_mu(Y).cpu().numpy().reshape(n_test, test_data.Y_dim)
    Y_orig_df = pd.DataFrame(Y_orig, columns=train_val_cfg.data.Y_cols)
    Y_orig_values = Y_orig_df[required_params].values # [n_test, Y_dim]

    # Optionally export the truth parameters Y, reverse-transformed
    if test_cfg.export.reverse_transformed_truth:
        pd.DataFrame(Y_orig_values, columns=required_params).to_csv(os.path.join(out_dir, 'Y_truth.csv'), index=False)

    # Optionally export the predictions, properly reverse-transformed
    if test_cfg.export.pred:
        # Primary mu
        pred_mu = bnn_post.transform_back_mu(pred[:, :test_data.Y_dim]).cpu().numpy().reshape(n_test, test_data.Y_dim)
        pred_mu_df = pd.DataFrame(pred_mu, columns=train_val_cfg.data.Y_cols)
        # Second gaussian weights
        pred_mu_df['w2'] = bnn_post.w2.cpu().numpy().squeeze()
        # Diagonal elements of first covariance matrix, square-rooted
        pred_cov_diag_sqrt = (bnn_post.cov_diag**0.5*bnn_post.Y_std.reshape([1, -1])).cpu().numpy()
        pred_cov_df = pd.DataFrame(pred_cov_diag_sqrt, columns=train_val_cfg.data.Y_cols)
        # Concat the two
        pred_df = pd.merge(left=pred_mu_df, right=pred_cov_df, how='inner', left_index=True, right_index=True, suffixes=('', '_sig2'))
        pred_df.to_csv(os.path.join(out_dir, 'pred.csv'), index=False)

    # Add artificial noise around the truth values
    if test_cfg.lens_posterior_type == 'truth':
        Y_orig_values = Y_orig_values[:, np.newaxis, :] # [n_test, 1, Y_dim]
        artificial_noise = np.random.randn(n_test, sampling_buffer*n_samples, test_data.Y_dim)*Y_orig_values*test_cfg.fractional_error_added_to_truth # [n_test, buffer*n_samples, Y_dim]
        lens_model_samples_values = Y_orig_values + artificial_noise # [n_test, buffer*n_samples, Y_dim]

    # Placeholders for mean and std of H0 samples per system
    mean_h0_set = np.zeros(n_test)
    std_h0_set = np.zeros(n_test)
    inference_time = np.zeros(n_test)
    # For each lens system...
    down()
    total_progress = progressbar.ProgressBar(maxval=n_test)
    total_progress.start()
    lens_i_start_time = time.time()
    for lens_i in range(n_test):
        up()
        # BNN samples for lens_i
        bnn_sample_df = pd.DataFrame(lens_model_samples_values[lens_i, :, :], columns=required_params)
        # Cosmology observables for lens_i
        cosmo = cosmo_df.iloc[lens_i]
        true_td = np.array(literal_eval(cosmo['true_td']))
        true_img_dec = np.trim_zeros(cosmo[['y_image_0', 'y_image_1', 'y_image_2', 'y_image_3']].values, 'b')
        true_img_ra = np.trim_zeros(cosmo[['x_image_0', 'x_image_1', 'x_image_2', 'x_image_3']].values, 'b')
        h0_post.set_cosmology_observables(
                                          z_lens=cosmo['z_lens'], 
                                          z_src=cosmo['z_src'], 
                                          measured_vd=cosmo['true_vd']*(1.0 + np.random.randn()*test_cfg.error_model.velocity_dispersion_frac_error), 
                                          measured_vd_err=test_cfg.velocity_dispersion_likelihood.sigma, 
                                          measured_td=true_td + np.random.randn(*true_td.shape)*test_cfg.error_model.time_delay_error,
                                          measured_td_err=test_cfg.time_delay_likelihood.sigma, 
                                          abcd_ordering_i=np.arange(len(true_td)),
                                          true_img_dec=true_img_dec,
                                          true_img_ra=true_img_ra,
                                          )
        # Initialize output array
        h0_samples = np.ones(n_samples)*-1 # -1 if the sample errored and was skipped
        h0_weights = np.zeros(n_samples)
        # For each sample from the lens model posterior of this lens system...
        sampling_progress = progressbar.ProgressBar(n_samples)
        sampling_progress.start()
        valid_sample_i = 0
        sample_i = 0
        while valid_sample_i < n_samples:
            if sample_i > sampling_buffer*n_samples - 1:
                break
            try:
                sampled_lens_model_raw = bnn_sample_df.iloc[sample_i]
                h0, weight = h0_post.get_h0_sample(sampled_lens_model_raw)
                h0_samples[valid_sample_i] = h0
                h0_weights[valid_sample_i] = weight
                sampling_progress.update(valid_sample_i + 1)
                time.sleep(0.001)
                valid_sample_i += 1
                sample_i += 1
            except:
                sample_i += 1
                continue
        lens_i_end_time = time.time()
        sampling_progress.finish()
        # Normalize weights to unity
        is_nan_mask = np.logical_or(np.isnan(h0_weights), ~np.isfinite(h0_weights))
        h0_weights[~is_nan_mask] = h0_weights[~is_nan_mask]/np.sum(h0_weights[~is_nan_mask])
        h0_dict = dict(
                       h0_samples=h0_samples[~is_nan_mask],
                       h0_weights=h0_weights[~is_nan_mask],
                       )
        h0_dict_save_path = os.path.join(out_dir, 'h0_dict_{0:04d}.npy'.format(lens_i))
        np.save(h0_dict_save_path, h0_dict)
        mean_h0, std_h0 = plot_h0_histogram(h0_samples[~is_nan_mask], h0_weights[~is_nan_mask], lens_i, cosmo['H0'], include_fit_gaussian=test_cfg.plotting.include_fit_gaussian, save_dir=out_dir)
        mean_h0_set[lens_i] = mean_h0
        std_h0_set[lens_i] = std_h0
        inference_time[lens_i] = (lens_i_end_time - lens_i_start_time)/60.0 # min
        total_progress.update(lens_i + 1)
    total_progress.finish()
    h0_stats = dict(
                    name='rung1_seed{:d}'.format(lens_i),
                    mean=mean_h0_set,
                    std=std_h0_set,
                    )
    h0_stats_save_path = os.path.join(out_dir, 'h0_stats')
    np.save(h0_stats_save_path, h0_stats)

if __name__ == '__main__':
    main()