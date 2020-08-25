# -*- coding: utf-8 -*-
"""Training the Bayesian neural network (BNN).
This script performs H0 inference on a test (or validation) sample using simple MC sampling

Example
-------
To run this script, pass in the path to the user-defined training config file as the argument::
    
    $ python h0rton/infer_h0_simple_mc.py h0rton/h0_inference_config.json

"""
import os
import sys
import argparse
import random
import time
from addict import Dict
from tqdm import tqdm
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
from baobab import BaobabConfig
# H0rton modules
from h0rton.configs import TrainValConfig, TestConfig
import h0rton.losses
import h0rton.train_utils as train_utils
import h0rton.script_utils as script_utils
from h0rton.h0_inference import H0Posterior, plot_weighted_h0_histogram
from h0rton.trainval_data import XYData

def main():
    args = script_utils.parse_inference_args()
    test_cfg = TestConfig.from_file(args.test_config_file_path)
    baobab_cfg = BaobabConfig.from_file(test_cfg.data.test_baobab_cfg_path)
    cfg = TrainValConfig.from_file(test_cfg.train_val_config_file_path)
    # Set device and default data type
    device = torch.device(test_cfg.device_type)
    if device.type == 'cuda':
        torch.set_default_tensor_type('torch.cuda.FloatTensor')
    else:
        torch.set_default_tensor_type('torch.FloatTensor')
    script_utils.seed_everything(test_cfg.global_seed)
    
    ############
    # Data I/O #
    ############
    train_data = XYData(is_train=True, 
                        Y_cols=cfg.data.Y_cols, 
                        float_type=cfg.data.float_type, 
                        define_src_pos_wrt_lens=cfg.data.define_src_pos_wrt_lens, 
                        rescale_pixels=cfg.data.rescale_pixels, 
                        log_pixels=cfg.data.log_pixels, 
                        add_pixel_noise=cfg.data.add_pixel_noise, 
                        eff_exposure_time=cfg.data.eff_exposure_time, 
                        train_Y_mean=None, 
                        train_Y_std=None, 
                        train_baobab_cfg_path=cfg.data.train_baobab_cfg_path, 
                        val_baobab_cfg_path=test_cfg.data.test_baobab_cfg_path, 
                        for_cosmology=False)
    # Define val data and loader
    test_data = XYData(is_train=False, 
                       Y_cols=cfg.data.Y_cols, 
                       float_type=cfg.data.float_type, 
                       define_src_pos_wrt_lens=cfg.data.define_src_pos_wrt_lens, 
                       rescale_pixels=cfg.data.rescale_pixels, 
                       log_pixels=cfg.data.log_pixels, 
                       add_pixel_noise=cfg.data.add_pixel_noise, 
                       eff_exposure_time=cfg.data.eff_exposure_time, 
                       train_Y_mean=train_data.train_Y_mean, 
                       train_Y_std=train_data.train_Y_std, 
                       train_baobab_cfg_path=cfg.data.train_baobab_cfg_path, 
                       val_baobab_cfg_path=test_cfg.data.test_baobab_cfg_path, 
                       for_cosmology=True)
    if test_cfg.data.lens_indices is None:
        n_test = test_cfg.data.n_test # number of lenses in the test set
        lens_range = range(n_test)
    else: # if specific lenses are specified
        lens_range = test_cfg.data.lens_indices
        n_test = len(lens_range)
        print("Performing H0 inference on {:d} specified lenses...".format(n_test))
    batch_size = max(lens_range) + 1
    test_loader = DataLoader(test_data, batch_size=batch_size, shuffle=False, drop_last=True)
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
    loss_fn = getattr(h0rton.losses, cfg.model.likelihood_class)(Y_dim=test_data.Y_dim, device=device)
    # Instantiate posterior (for logging)
    bnn_post = getattr(h0rton.h0_inference.gaussian_bnn_posterior, loss_fn.posterior_name)(test_data.Y_dim, device, train_data.train_Y_mean, train_data.train_Y_std)
    # Instantiate model
    net = getattr(h0rton.models, cfg.model.architecture)(num_classes=loss_fn.out_dim)
    net.to(device)
    # Load trained weights from saved state
    net, epoch = train_utils.load_state_dict_test(test_cfg.state_dict_path, net, cfg.optim.n_epochs, device)
    dropout_samples = test_data.Y_dim + 1
    n_samples_per_dropout = test_cfg.numerics.n_samples_per_dropout
    init_pos = np.empty([batch_size, dropout_samples, n_samples_per_dropout, test_data.Y_dim]) 
    mcmc_pred = 0.0 # initialize zero array with shape unknown a priori
    with torch.no_grad():
        for d in range(dropout_samples):
            net.eval()
            for X_, Y_ in test_loader:
                X = X_.to(device)
                Y = Y_.to(device) # TODO: compare master_truth with reverse-transformed Y
                pred = net(X)
                break
            mcmc_pred_d = pred.cpu().numpy()
            mcmc_pred += mcmc_pred_d/dropout_samples
            # Instantiate posterior for BNN samples, to initialize the walkers
            bnn_post = getattr(h0rton.h0_inference.gaussian_bnn_posterior, loss_fn.posterior_name)(test_data.Y_dim, device, mcmc_train_Y_mean, mcmc_train_Y_std)
            bnn_post.set_sliced_pred(torch.tensor(mcmc_pred_d))
            init_pos[:, d, :, :] = bnn_post.sample(n_samples_per_dropout, sample_seed=test_cfg.global_seed+d) # [batch_size, dropout_samples, n_samples_per_dropout, test_data.Y_dim] contains just the lens model params, no D_dt
            gc.collect()

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
                        lens_model_list=['PEMD', 'SHEAR'],
                        lens_light_model_list=['SERSIC_ELLIPSE'],
                        source_light_model_list=['SERSIC_ELLIPSE'],
                        point_source_model_list=['SOURCE_POSITION'],
                        cosmo=FlatLambdaCDM(H0=70.0, Om0=0.3) # fiducial model
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
                          kappa_transformed=test_cfg.kappa_ext_prior.transformed,
                          Om0=baobab_cfg.bnn_omega.cosmology.Om0,
                          define_src_pos_wrt_lens=cfg.data.define_src_pos_wrt_lens,
                          kwargs_lens_eqn_solver={'min_distance': 0.05, 'search_window': baobab_cfg.instrument['pixel_scale']*baobab_cfg.image['num_pix'], 'num_iter_max': 100},
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
    sampling_buffer = test_cfg.h0_posterior.sampling_buffer # FIXME: dynamically sample more if we run out of samples
    actual_n_samples = int(n_samples*sampling_buffer)

    if test_cfg.lens_posterior_type == 'bnn':
        # Sample from the BNN posterior
        
        lens_model_samples = bnn_post.sample(actual_n_samples, sample_seed=test_cfg.global_seed).reshape(-1, test_data.Y_dim) # [n_test*n_samples, Y_dim]
        lens_model_samples_df = pd.DataFrame(lens_model_samples, columns=cfg.data.Y_cols)
        lens_model_samples_values = lens_model_samples_df[required_params].values.reshape(batch_size, actual_n_samples, -1)
        # Optionally export the BNN predictions, properly reverse-transformed
        if test_cfg.export.pred:
            # Primary mu
            pred_mu = bnn_post.transform_back_mu(pred[:, :test_data.Y_dim]).cpu().numpy().reshape(n_test, test_data.Y_dim)
            pred_mu_df = pd.DataFrame(pred_mu, columns=cfg.data.Y_cols)
            # Second gaussian weights
            pred_mu_df['w2'] = bnn_post.w2.cpu().numpy().squeeze()
            # Diagonal elements of first covariance matrix, square-rooted
            pred_cov_diag_sqrt = (bnn_post.cov_diag**0.5*bnn_post.Y_std.reshape([1, -1])).cpu().numpy()
            pred_cov_df = pd.DataFrame(pred_cov_diag_sqrt, columns=cfg.data.Y_cols)
            # Concat the two
            pred_df = pd.merge(left=pred_mu_df, right=pred_cov_df, how='inner', left_index=True, right_index=True, suffixes=('', '_sig2'))
            pred_df.to_csv(os.path.join(out_dir, 'pred.csv'), index=False)
    elif 'truth' in test_cfg.lens_posterior_type:
        raise ValueError("Please run the infer_h0_simple_mc_truth.py script instead.")
    elif test_cfg.lens_posterior_type == 'hybrid':
        raise ValueError("Please run the infer_h0_hybrid.py script instead.")
    else:
        raise NotImplementedError("Lens posterior types of bnn, truth, hybrid supported.")

    # Placeholders for mean and std of H0 samples per system
    mean_h0_set = np.zeros(n_test)
    std_h0_set = np.zeros(n_test)
    inference_time_set = np.zeros(n_test)
    realized_time_delays = pd.read_csv('/home/jwp/stage/sl/h0rton/experiments/v27/summary.csv', index_col=None)
    # For each lens system...
    total_progress = tqdm(total=n_test)
    sampling_progress = tqdm(total=n_samples)
    for i, lens_i in enumerate(lens_range):
        lens_i_start_time = time.time()
        # Each lens gets a unique random state for td and vd measurement error realizations.
        rs_lens = np.random.RandomState(lens_i)
        # BNN samples for lens_i
        bnn_sample_df = pd.DataFrame(lens_model_samples_values[lens_i, :, :], columns=required_params)
        # Cosmology observables for lens_i
        cosmo = cosmo_df.iloc[lens_i]
        true_td = np.array(literal_eval(cosmo['true_td']))
        true_img_dec = np.array(literal_eval(cosmo['y_image']))
        true_img_ra = np.array(literal_eval(cosmo['x_image']))
        measured_vd = cosmo['true_vd']*(1.0 + rs_lens.randn()*test_cfg.error_model.velocity_dispersion_frac_error)

        #measured_td = true_td + rs_lens.randn(*true_td.shape)*test_cfg.error_model.time_delay_error
        #print(true_td, measured_td)
        measured_td_wrt0 = np.array(literal_eval(realized_time_delays.iloc[lens_i]['measured_td_wrt0']))
        h0_post.set_cosmology_observables(
                                          z_lens=cosmo['z_lens'], 
                                          z_src=cosmo['z_src'], 
                                          measured_vd=measured_vd, 
                                          measured_vd_err=test_cfg.velocity_dispersion_likelihood.sigma, 
                                          measured_td_wrt0=measured_td_wrt0,
                                          measured_td_err=test_cfg.time_delay_likelihood.sigma, 
                                          abcd_ordering_i=np.arange(len(true_td)),
                                          true_img_dec=true_img_dec,
                                          true_img_ra=true_img_ra,
                                          )
        # Initialize output array
        h0_samples = np.full(n_samples, np.nan)
        h0_weights = np.zeros(n_samples)
        # For each sample from the lens model posterior of this lens system...
        sampling_progress.reset()
        valid_sample_i = 0
        sample_i = 0
        while valid_sample_i < n_samples:
            if sample_i > actual_n_samples - 1:
                break
            try:
                # Each sample for a given lens gets a unique random state for H0, k_ext, and aniso_param realizations.
                rs_sample = np.random.RandomState(int(str(lens_i) + str(sample_i)))
                sampled_lens_model_raw = bnn_sample_df.iloc[sample_i]
                h0, weight = h0_post.get_h0_sample(sampled_lens_model_raw, rs_sample)
                h0_samples[valid_sample_i] = h0
                h0_weights[valid_sample_i] = weight
                sampling_progress.update(1)
                time.sleep(0.001)
                valid_sample_i += 1
                sample_i += 1
            except:
                sample_i += 1
                continue
        sampling_progress.refresh()
        lens_i_end_time = time.time()
        inference_time = (lens_i_end_time - lens_i_start_time)/60.0 # min
        h0_dict = dict(
                       h0_samples=h0_samples,
                       h0_weights=h0_weights,
                       n_sampling_attempts=sample_i,
                       inference_time=inference_time
                       )
        h0_dict_save_path = os.path.join(out_dir, 'h0_dict_{0:04d}.npy'.format(lens_i))
        np.save(h0_dict_save_path, h0_dict)
        mean_h0, std_h0 = plot_weighted_h0_histogram(h0_samples, h0_weights, lens_i, cosmo['H0'], include_fit_gaussian=test_cfg.plotting.include_fit_gaussian, save_dir=out_dir)
        mean_h0_set[i] = mean_h0
        std_h0_set[i] = std_h0
        inference_time_set[i] = inference_time
        total_progress.update(1)
    total_progress.close()
    h0_stats = dict(
                    mean=mean_h0_set,
                    std=std_h0_set,
                    inference_time=inference_time_set,
                    )
    h0_stats_save_path = os.path.join(out_dir, 'h0_stats')
    np.save(h0_stats_save_path, h0_stats)

if __name__ == '__main__':
    main()