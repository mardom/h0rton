"""Script to run an MCMC afterburner for the BNN posterior

It borrows heavily from the `catalogue modelling.ipynb` notebook in Lenstronomy Extensions, which you can find `here <https://github.com/sibirrer/lenstronomy_extensions/blob/master/lenstronomy_extensions/Notebooks/catalogue%20modelling.ipynb>`_.

"""
import os
import time
from tqdm import tqdm
from ast import literal_eval
import numpy as np
import pandas as pd
import torch
from torch.utils.data import DataLoader
from lenstronomy.Workflow.fitting_sequence import FittingSequence
from lenstronomy.Cosmo.lcdm import LCDM
import baobab.sim_utils.metadata_utils as metadata_utils
from h0rton.script_utils import parse_args, seed_everything, HiddenPrints
import h0rton.models
from h0rton.configs import TrainValConfig, TestConfig
import h0rton.losses
import h0rton.train_utils as train_utils
from h0rton.h0_inference import h0_utils, plotting_utils, mcmc_utils
from h0rton.trainval_data import XYCosmoData

def main():
    args = parse_args()
    test_cfg = TestConfig.from_file(args.test_config_file_path)
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
    master_truth = test_data.cosmo_df
    master_truth = metadata_utils.add_qphi_columns(master_truth)
    master_truth = metadata_utils.add_gamma_psi_ext_columns(master_truth)
    if test_cfg.data.lens_indices is None:
        if args.lens_indices_path is None:
            # Test on all n_test lenses in the test set
            n_test = test_cfg.data.n_test 
            lens_range = range(n_test)
        else:
            # Test on the lens indices in a text file at the specified path
            lens_range = []
            with open(args.lens_indices_path, "r") as f:
                for line in f:
                    lens_range.append(int(line.strip()))
            n_test = len(lens_range)
            print("Performing H0 inference on {:d} specified lenses...".format(n_test))
    else:
        if args.lens_indices_path is None:
            # Test on the lens indices specified in the test config file
            lens_range = test_cfg.data.lens_indices
            n_test = len(lens_range)
            print("Performing H0 inference on {:d} specified lenses...".format(n_test))
        else:
            raise ValueError("Specific lens indices were specified in both the test config file and the command-line argument.")
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
    orig_Y_cols = train_val_cfg.data.Y_cols
    loss_fn = getattr(h0rton.losses, train_val_cfg.model.likelihood_class)(Y_dim=train_val_cfg.data.Y_dim, device=device)
    # Instantiate MCMC parameter penalty function
    params_to_remove = ['lens_light_R_sersic', 'src_light_R_sersic'] # must be removed, as post-processing scheme doesn't optimize them
    mcmc_Y_cols = [col for col in orig_Y_cols if col not in params_to_remove]
    mcmc_loss_fn = getattr(h0rton.losses, train_val_cfg.model.likelihood_class)(Y_dim=train_val_cfg.data.Y_dim - len(params_to_remove), device=device)
    remove_param_idx, remove_idx = mcmc_utils.get_idx_for_params(mcmc_loss_fn.out_dim, orig_Y_cols, params_to_remove, train_val_cfg.model.likelihood_class)
    mcmc_train_Y_mean = np.delete(train_val_cfg.data.train_Y_mean, remove_param_idx)
    mcmc_train_Y_std = np.delete(train_val_cfg.data.train_Y_std, remove_param_idx)
    parameter_penalty = mcmc_utils.HybridBNNPenalty(mcmc_Y_cols, train_val_cfg.model.likelihood_class, mcmc_train_Y_mean, mcmc_train_Y_std, test_cfg.h0_posterior.exclude_velocity_dispersion, device)
    custom_logL_addition = parameter_penalty.evaluate if test_cfg.lens_posterior_type.startswith('default') else None
    # Instantiate model
    net = getattr(h0rton.models, train_val_cfg.model.architecture)(num_classes=loss_fn.out_dim)
    net.to(device)
    # Load trained weights from saved state
    net, epoch = train_utils.load_state_dict_test(test_cfg.state_dict_path, net, train_val_cfg.optim.n_epochs, device)
    with torch.no_grad():
        net.eval()
        for X_, Y_ in test_loader:
            X = X_.to(device)
            Y = Y_.to(device) # TODO: compare master_truth with reverse-transformed Y
            pred = net(X)
            break
    
    mcmc_pred = pred.cpu().numpy()
    if test_cfg.lens_posterior_type == 'default_with_truth_mean':
        # Replace BNN posterior's primary gaussian mean with truth values
        mcmc_pred[:, :len(mcmc_Y_cols)] = Y[:, :len(mcmc_Y_cols)].cpu().numpy()
    mcmc_pred = mcmc_utils.remove_parameters_from_pred(mcmc_pred, remove_idx, return_as_tensor=False)

    kwargs_model = dict(lens_model_list=['SPEMD', 'SHEAR'],
                        point_source_model_list=['SOURCE_POSITION'],)
    astro_sig = test_cfg.image_position_likelihood.sigma
    # Get H0 samples for each system
    if not test_cfg.time_delay_likelihood.baobab_time_delays:
        if 'abcd_ordering_i' not in master_truth:
            raise ValueError("If the time delay measurements were not generated using Baobab, the user must specify the order of image positions in which the time delays are listed, in order of increasing dec.")

    total_progress = tqdm(total=n_test)
    # For each lens system...
    for i, lens_i in enumerate(lens_range):
        # Each lens gets a unique random state for td and vd measurement error realizations.
        rs_lens = np.random.RandomState(lens_i)
        ###########################
        # Relevant data and prior #
        ###########################
        data_i = master_truth.iloc[lens_i].copy()
        parameter_penalty.set_bnn_post_params(mcmc_pred[lens_i, :]) # set the BNN parameters
        # Init values for the lens model params
        if test_cfg.lens_posterior_type == 'default':
            init_info = dict(zip(mcmc_Y_cols, mcmc_pred[lens_i, :len(mcmc_Y_cols)]*mcmc_train_Y_std + mcmc_train_Y_mean)) # mean of primary Gaussian
        else: # types 'hybrid_with_truth_mean' and 'truth'
            init_info = dict(zip(mcmc_Y_cols, data_i[mcmc_Y_cols].values)) # truth params
        if not test_cfg.h0_posterior.exclude_velocity_dispersion:
            parameter_penalty.set_vel_disp_params()
            raise NotImplementedError
        lcdm = LCDM(z_lens=data_i['z_lens'], z_source=data_i['z_src'], flat=True)
        true_img_dec = np.trim_zeros(data_i[['y_image_0', 'y_image_1', 'y_image_2', 'y_image_3']].values, 'b')
        n_img = len(true_img_dec)
        true_td = np.array(literal_eval(data_i['true_td']))
        measured_td = true_td + rs_lens.randn(*true_td.shape)*test_cfg.error_model.time_delay_error
        measured_td_sig = test_cfg.time_delay_likelihood.sigma # np.ones(n_img - 1)*
        measured_img_dec = true_img_dec + rs_lens.randn(n_img)*astro_sig
        increasing_dec_i = np.argsort(measured_img_dec)
        measured_td = h0_utils.reorder_to_tdlmc(measured_td, increasing_dec_i, range(n_img)) # need to use measured dec to order
        measured_img_dec = h0_utils.reorder_to_tdlmc(measured_img_dec, increasing_dec_i, range(n_img))
        measured_td_wrt0 = measured_td[1:] - measured_td[0]   
        kwargs_data_joint = dict(time_delays_measured=measured_td_wrt0,
                                 time_delays_uncertainties=measured_td_sig,
                                 #vel_disp_measured=measured_vd, # TODO: optionally exclude
                                 #vel_disp_uncertainty=vel_disp_sig,
                                 )
        if not test_cfg.h0_posterior.exclude_velocity_dispersion:
            measured_vd = data_i['true_vd']*(1.0 + rs_lens.randn()*test_cfg.error_model.velocity_dispersion_frac_error)
            kwargs_data_joint['vel_disp_measured'] = measured_vd
            kwargs_data_joint['vel_disp_sig'] = test_cfg.velocity_dispersion_likelihood.sigma

        #############################
        # Parameter init and bounds #
        #############################
        lens_kwargs = mcmc_utils.get_lens_kwargs(init_info)
        ps_kwargs = mcmc_utils.get_ps_kwargs_src_plane(init_info, astro_sig)
        special_kwargs = mcmc_utils.get_special_kwargs(n_img, astro_sig) # image position offset and time delay distance, aka the "special" parameters
        kwargs_params = {'lens_model': lens_kwargs,
                         'point_source_model': ps_kwargs,
                         'special': special_kwargs,}
        if test_cfg.numerics.solver_type == 'NONE':
            solver_type = 'NONE'
        else:
            solver_type = 'PROFILE_SHEAR' if n_img == 4 else 'CENTER'
        #solver_type = 'NONE'
        kwargs_constraints = {'num_point_source_list': [n_img],  
                              'Ddt_sampling': True,
                              'solver_type': solver_type,}

        kwargs_likelihood = {'time_delay_likelihood': True,
                             'sort_images_by_dec': True,
                             'prior_lens': [],
                             'prior_special': [],
                             'check_bounds': True, 
                             'source_position_tolerance': 0.001,
                             'source_position_sigma': 0.001,
                             'source_position_likelihood': False,
                             'custom_logL_addition': custom_logL_addition,}

        ###########################
        # MCMC posterior sampling #
        ###########################
        fitting_seq = FittingSequence(kwargs_data_joint, kwargs_model, kwargs_constraints, kwargs_likelihood, kwargs_params, verbose=False)
        # MCMC sample from the post-processed BNN posterior jointly with cosmology
        lens_i_start_time = time.time()
        fitting_kwargs_list_mcmc = [['MCMC', test_cfg.numerics.mcmc]]
        with HiddenPrints():
            chain_list_mcmc = fitting_seq.fit_sequence(fitting_kwargs_list_mcmc)
            kwargs_result_mcmc = fitting_seq.best_fit()
        lens_i_end_time = time.time()
        inference_time = (lens_i_end_time - lens_i_start_time)/60.0 # min

        #############################
        # Plotting the MCMC samples #
        #############################
        # sampler_type : 'EMCEE'
        # samples_mcmc : np.array of shape `[n_mcmc_eval, n_params]`
        # param_mcmc : list of str of length n_params, the parameter names
        sampler_type, samples_mcmc, param_mcmc, _  = chain_list_mcmc[0]
        new_samples_mcmc = mcmc_utils.postprocess_mcmc_chain(kwargs_result_mcmc, samples_mcmc, kwargs_model, lens_kwargs[2], ps_kwargs[2], special_kwargs[2], kwargs_constraints)
        # Plot D_dt histogram
        D_dt_samples = new_samples_mcmc['D_dt'].values
        true_D_dt = lcdm.D_dt(H_0=data_i['H0'], Om0=0.3)
        data_i['D_dt'] = true_D_dt
        # Export D_dt samples for this lens
        lens_inference_dict = dict(
                                   D_dt_samples=D_dt_samples, # kappa_ext=0 for these samples
                                   inference_time=inference_time,
                                   true_D_dt=true_D_dt, 
                                   )
        lens_inference_dict_save_path = os.path.join(out_dir, 'D_dt_dict_{0:04d}.npy'.format(lens_i))
        np.save(lens_inference_dict_save_path, lens_inference_dict)
        # Optionally export the D_dt histogram
        if test_cfg.export.D_dt_histogram:
            _ = plotting_utils.plot_D_dt_histogram(D_dt_samples, lens_i, true_D_dt, save_dir=out_dir)
        # Optionally export the plot of MCMC chain
        if test_cfg.export.mcmc_chain:
            mcmc_chain_path = os.path.join(out_dir, 'mcmc_chain_{0:04d}.png'.format(lens_i))
            plotting_utils.plot_mcmc_chain(chain_list_mcmc, mcmc_chain_path)
        # Optionally export posterior cornerplot of select lens model parameters with D_dt
        if test_cfg.export.mcmc_corner:
            mcmc_corner_path = os.path.join(out_dir, 'mcmc_corner_{0:04d}.png'.format(lens_i))
            plotting_utils.plot_mcmc_corner(new_samples_mcmc[test_cfg.export.mcmc_cols], data_i[test_cfg.export.mcmc_cols], test_cfg.export.mcmc_col_labels, mcmc_corner_path)
        total_progress.update(1)
    total_progress.close()

if __name__ == '__main__':
    import cProfile
    pr = cProfile.Profile()
    pr.enable()
    main()
    pr.disable()
    pr.print_stats(sort='cumtime')