# #########################################################################
# Copyright (c) , UChicago Argonne, LLC. All rights reserved.             #
#                                                                         #
# See LICENSE file.                                                       #
# #########################################################################


"""
cohere_core.phasing
===================

Provides phasing capabilities for the Bragg CDI data.
The software can run code utilizing different library, such as numpy and cupy. User configures the choice depending on hardware and installed software.

"""

from pathlib import Path
import time
import os
import sys
from math import pi
import random
import tqdm

import numpy as np
from matplotlib import pyplot as plt
import tifffile as tf

import cohere_core.utilities.dvc_utils as dvut
import cohere_core.utilities.utils as ut
import cohere_core.utilities.config_verifier as ver
import cohere_core.controller.op_flow as of
import cohere_core.controller.features as ft

__author__ = "Barbara Frosik"
__copyright__ = "Copyright (c) 2016, UChicago Argonne, LLC."
__docformat__ = 'restructuredtext en'
__all__ = ['set_lib_from_pkg',
           'reconstruction',
           'Breeder',
           'Rec',
           'DataRec']


def set_lib_from_pkg(pkg):
    global devlib

    # get the lib object
    devlib = ut.set_lib(pkg)
    # pass the lib object to the features associated with this reconstruction
    ft.set_lib(devlib)
    # the utilities are not associated with reconstruction and the initialization of lib is independent
    dvut.set_lib_from_pkg(pkg)


class Breeder:
    def __init__(self, alpha_dir):
        self.alpha_dir = alpha_dir

    def breed(self, phaser, breed_mode, threshold, sigma):
        if breed_mode != 'none':
            try:
                phaser.ds_image = dvut.breed(breed_mode, self.alpha_dir, phaser.ds_image)
                phaser.support = dvut.shrink_wrap(phaser.ds_image, threshold, sigma)
            except:
                print('exception during breeding')
                return -1

        return 0


class Rec:
    """
    cohere_core.phasing.reconstruction(self, params, data_file)

    Class, performs phasing using iterative algorithm.

    params : dict
        parameters used in reconstruction. Refer to x for parameters description
    data_file : str
        name of file containing data to be reconstructed

    """
    __all__ = []
    def __init__(self, params, data_file, pkg):
        set_lib_from_pkg(pkg)
        self.iter_functions = [self.next,
                               self.lowpass_filter_operation,
                               self.reset_resolution,
                               self.shrink_wrap_operation,
                               self.phc_operation,
                               self.to_reciprocal_space,
                               self.new_func_operation,
                               self.pc_operation,
                               self.pc_modulus,
                               self.modulus,
                               self.set_prev_pc,
                               self.to_direct_space,
                               self.er,
                               self.hio,
                               self.new_alg,
                               self.twin_operation,
                               self.average_operation,
                               self.progress_operation]

        params['init_guess'] = params.get('init_guess', 'random')
        if params['init_guess'] == 'AI_guess':
            if 'AI_threshold' not in params:
                params['AI_threshold'] = params['shrink_wrap_threshold']
            if 'AI_sigma' not in params:
                params['AI_sigma'] = params['shrink_wrap_gauss_sigma']
        params['reconstructions'] = params.get('reconstructions', 1)
        params['hio_beta'] = params.get('hio_beta', 0.9)
        params['initial_support_area'] = params.get('initial_support_area', (.5, .5, .5))
        if 'twin_trigger' in params:
            params['twin_halves'] = params.get('twin_halves', (0, 0))
        if 'pc_interval' in params and 'pc' in params['algorithm_sequence']:
            self.is_pcdi = True
        else:
            self.is_pcdi = False
        # finished setting defaults
        self.params = params
        self.data_file = data_file
        self.ds_image = None
        self.need_save_data = False
        self.saved_data = None
        self.er_iter = False  # Indicates whether the last iteration done was ER, used in CoupledRec
        self.progress_callback = None # Method called in progress_operation()

    def init_dev(self, device_id):
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        if device_id != -1:
            self.dev = device_id
            if device_id != -1:
                try:
                    devlib.set_device(device_id)
                except Exception as e:
                    print(e)
                    print('may need to restart GUI')
                    return -1

        if self.data_file.endswith('tif') or self.data_file.endswith('tiff'):
            try:
                data_np = ut.read_tif(self.data_file)
                data = devlib.from_numpy(data_np)
            except Exception as e:
                print(e)
                return -1
        elif self.data_file.endswith('npy'):
            try:
                data = devlib.load(self.data_file)
            except Exception as e:
                print(e)
                return -1
        else:
            print('no data file found')
            return -1
        # in the formatted data the max is in the center, we want it in the corner, so do fft shift
        self.data = devlib.fftshift(devlib.absolute(data))
        self.dims = devlib.dims(self.data)
        print('data shape', self.dims)

        if self.need_save_data:
            self.saved_data = devlib.copy(self.data)
            self.need_save_data = False

        return 0


    def init(self, dir=None, alpha_dir=None, gen=None):
        def create_feat_objects(params, trig_op_info):
            if 'shrink_wrap_trigger' in params:
                self.shrink_wrap_obj = ft.create('shrink_wrap', params, trig_op_info)
                if self.shrink_wrap_obj is None:
                    print('failed to create shrink wrap object')
                    return False
            if 'phc_trigger' in params:
                self.phc_obj = ft.create('phc', params, trig_op_info)
                if self.phc_obj is None:
                    print('failed to create phase constrain object')
                    return False
            if 'lowpass_filter_trigger' in params:
                self.lowpass_filter_obj = ft.create('lowpass_filter', params, trig_op_info)
                if self.lowpass_filter_obj is None:
                    print('failed to create lowpass filter object')
                    return False
            return True

        if self.ds_image is not None:
            first_run = False
        elif dir is None or not os.path.isfile(ut.join(dir, 'image.npy')):
            self.ds_image = devlib.random(self.dims, dtype=self.data.dtype)
            first_run = True
        else:
            self.ds_image = devlib.load(ut.join(dir, 'image.npy'))
            first_run = False

        # When running GA the lowpass filter, phc, and twin triggers should be active only during first
        # generation. The code below inactivates the triggers in subsequent generations.
        # This will be removed in the future when each generation will have own configuration.
        if not first_run:
            self.params.pop('lowpass_filter_trigger', None)
            self.params.pop('phc_trigger', None)
            self.params.pop('twin_trigger', None)

        self.flow_items_list = [f.__name__ for f in self.iter_functions]

        self.is_pc, flow, feats = of.get_flow_arr(self.params, self.flow_items_list, gen)
        if flow is None:
            return -1

        self.flow = []
        (op_no, self.iter_no) = flow.shape
        for i in range(self.iter_no):
            for j in range(op_no):
                if flow[j, i] > 0:
                    self.flow.append(self.iter_functions[j])

        self.aver = None
        self.iter = -1
        self.errs = []
        self.gen = gen
        self.prev_dir = dir
        self.alpha_dir = alpha_dir

        # create or get initial support
        if dir is None or not os.path.isfile(ut.join(dir, 'support.npy')):
            init_support = [int(self.params['initial_support_area'][i] * self.dims[i]) for i in range(len(self.dims))]
            center = devlib.full(init_support, 1)
            self.support = dvut.pad_around(center, self.dims, 0)
        else:
            self.support = devlib.load(ut.join(dir, 'support.npy'))

        if self.is_pc:
            self.pc_obj = ft.Pcdi(self.params, self.data, dir)
        # create the object even if the feature inactive, it will be empty
        # If successful, it will return True, otherwise False
        if not create_feat_objects(self.params, feats):
            return -1

        # for the fast GA the data needs to be saved, as it would be changed by each lr generation
        # for non-fast GA the Rec object is created in each generation with the initial data
        if self.saved_data is not None:
            if self.params['low_resolution_generations'] > self.gen:
                self.data = devlib.gaussian_filter(self.saved_data, self.params['ga_lpf_sigmas'][self.gen])
            else:
                self.data = self.saved_data
        else:
            if self.gen is not None and self.params['low_resolution_generations'] > self.gen:
                self.data = devlib.gaussian_filter(self.data, self.params['ga_lpf_sigmas'][self.gen])

        if 'lowpass_filter_range' not in self.params or not first_run:
            self.iter_data = self.data
        else:
            self.iter_data = devlib.copy(self.data)

        if (first_run):
            max_data = devlib.amax(self.data)
            self.ds_image *= dvut.get_norm(self.ds_image) * max_data

            # the line below are for testing to set the initial guess to support
            # self.ds_image = devlib.full(self.dims, 1.0) + 1j * devlib.full(self.dims, 1.0)

            self.ds_image *= self.support
        return 0


    def clean_breeder(self):
        self.breeder = None
        devlib.clean_default_mem()


    def breed(self):
        def breed_retry():
            breed_mode = self.params['ga_breed_modes'][self.gen]
            threshold = self.params['ga_sw_thresholds'][self.gen]
            sigma = self.params['ga_sw_gauss_sigmas'][self.gen]
            return self.breeder.breed(self, breed_mode, threshold, sigma)
        try:
            return breed_retry()
        except Exception as e:
            # retry
            self.breeder = Breeder(self.alpha_dir)
            return breed_retry()


    def iterate(self):
        self.iter = -1
        start_t = time.time()
        try:
            for f in self.flow:
                f()
        except Exception as error:
            print(error)
            return -1

        if devlib.hasnan(self.ds_image):
            print('reconstruction resulted in NaN')
            return -1

        print('iterate took ', (time.time() - start_t), ' sec')

        if self.aver is not None:
            ratio = self.get_ratio(devlib.from_numpy(self.aver), devlib.absolute(self.ds_image))
            self.ds_image *= ratio / self.aver_iter

        mx = devlib.amax(devlib.absolute(self.ds_image))
        self.ds_image = self.ds_image / mx

        return 0

    def save_res(self, save_dir, only_image=False):
        # center image's center_of_mass and sync support
        self.ds_image, self.support = dvut.center_sync(self.ds_image, self.support)

        from array import array

        if not os.path.exists(save_dir):
            os.makedirs(save_dir)
        devlib.save(ut.join(save_dir, 'image'), self.ds_image)
        ut.save_tif(devlib.to_numpy(self.ds_image), ut.join(save_dir, 'image.tif'))

        if only_image:
            return 0

        devlib.save(ut.join(save_dir, 'support'), self.support)
        if self.is_pc:
            devlib.save(ut.join(save_dir, 'coherence'), self.pc_obj.kernel)
        errs = array('f', self.errs)

        with open(ut.join(save_dir, 'errors.txt'), 'w+') as err_f:
            err_f.write('\n'.join(map(str, errs)))

        devlib.save(ut.join(save_dir, 'errors'), errs)

        metric = dvut.all_metrics(self.ds_image, self.errs)
        with open(ut.join(save_dir, 'metrics.txt'), 'w+') as f:
            f.write(str(metric))

        return 0

    def get_metric(self, metric_type):
        return dvut.all_metrics(self.ds_image, self.errs)

    def next(self):
        self.iter = self.iter + 1

    def lowpass_filter_operation(self):
        args = (self.data, self.iter, self.ds_image)
        (self.iter_data, self.support) = self.lowpass_filter_obj.apply_trigger(*args)

    def reset_resolution(self):
        self.iter_data = self.data

    def shrink_wrap_operation(self):
        args = (self.ds_image,)
        self.support = self.shrink_wrap_obj.apply_trigger(*args)

    def phc_operation(self):
        args = (self.ds_image,)
        self.support *= self.phc_obj.apply_trigger(*args)

    def to_reciprocal_space(self):
        self.rs_amplitudes = devlib.ifft(self.ds_image)

    def new_func_operation(self):
        self.params['new_param'] = 1
        print(f'in new_func_trigger, new_param {self.params["new_param"]}')

    def pc_operation(self):
        self.pc_obj.update_partial_coherence(devlib.absolute(self.rs_amplitudes))

    def pc_modulus(self):
        abs_amplitudes = devlib.absolute(self.rs_amplitudes)
        converged = self.pc_obj.apply_partial_coherence(abs_amplitudes)
        ratio = self.get_ratio(self.iter_data, devlib.absolute(converged))
        error = dvut.get_norm(
            devlib.where(devlib.absolute(converged) != 0.0, devlib.absolute(converged) - self.iter_data, 0.0)) / dvut.get_norm(self.iter_data)
        self.errs.append(error)
        self.rs_amplitudes *= ratio

    def modulus(self):
        ratio = self.get_ratio(self.iter_data, devlib.absolute(self.rs_amplitudes))
        error = dvut.get_norm(devlib.where((self.rs_amplitudes != 0), (devlib.absolute(self.rs_amplitudes) - self.iter_data),
                                      0)) / dvut.get_norm(self.iter_data)
        self.errs.append(error)
        self.rs_amplitudes *= ratio

    def set_prev_pc(self):
        self.pc_obj.set_previous(devlib.absolute(self.rs_amplitudes))

    def to_direct_space(self):
        self.ds_image_raw = devlib.fft(self.rs_amplitudes)

    def er(self):
        self.er_iter = True
        self.ds_image = self.ds_image_raw * self.support

    def hio(self):
        self.er_iter = False
        combined_image = self.ds_image - self.ds_image_raw * self.params['hio_beta']
        self.ds_image = devlib.where((self.support > 0), self.ds_image_raw, combined_image)

    def new_alg(self):
        self.ds_image = 2.0 * (self.ds_image_raw * self.support) - self.ds_image_raw

    def twin_operation(self):
        # TODO this will work only for 3D array, but will the twin be used for 1D or 2D?
        # com = devlib.center_of_mass(devlib.absolute(self.ds_image))
        # sft = [int(self.dims[i] / 2 - com[i]) for i in range(len(self.dims))]
        # self.ds_image = devlib.shift(self.ds_image, sft)
        dims = devlib.dims(self.ds_image)
        half_x = int((dims[0] + 1) / 2)
        half_y = int((dims[1] + 1) / 2)
        if self.params['twin_halves'][0] == 0:
            self.ds_image[half_x:, :, :] = 0
        else:
            self.ds_image[: half_x, :, :] = 0
        if self.params['twin_halves'][1] == 0:
            self.ds_image[:, half_y:, :] = 0
        else:
            self.ds_image[:, : half_y, :] = 0

    def average_operation(self):
        if self.aver is None:
            self.aver = devlib.to_numpy(devlib.absolute(self.ds_image))
            self.aver_iter = 1
        else:
            self.aver = self.aver + devlib.to_numpy(devlib.absolute(self.ds_image))
            self.aver_iter += 1

    def progress_operation(self):
        print(f'------iter {self.iter}   error {self.errs[-1]}')
        if self.progress_callback:
            images = self.get_rec_data()
            self.progress_callback(images)

    def get_rec_data(self):
        ds_image, support = dvut.center_sync(self.ds_image, self.support)
        image = np.abs(devlib.to_numpy(ds_image)).transpose()
        return image

    def get_ratio(self, divident, divisor):
        ratio = devlib.where((divisor > 1e-9), divident / divisor, 0.0)
        return ratio

    def make_binary(self):
        self.support = devlib.absolute(self.support)
        self.support = devlib.where(self.support >= 0.9 * devlib.amax(self.support), 1, 0).astype("?")


def resample(data, matrix, size=None, plot=False):
    s = data.shape
    corners = [
        [0, 0, 0],
        [s[0], 0, 0], [0, s[1], 0], [0, 0, s[2]],
        [0, s[1], s[2]], [s[0], 0, s[2]], [s[0], s[1], 0],
        [s[0], s[1], s[2]]
    ]
    new_corners = [np.linalg.inv(matrix)@c for c in corners]
    new_shape = np.ceil(np.max(new_corners, axis=0) - np.min(new_corners, axis=0)).astype(int)
    pad = np.max((new_shape - s) // 2)
    data = devlib.pad(data, np.max([pad, 0]))
    mid_shape = np.array(data.shape)

    offset = (mid_shape / 2) - matrix @ (mid_shape / 2)

    # The phasing data as saved is a magnitude, which is not smooth everywhere (there are non-differentiable points
    # where the complex amplitude crosses zero). However, the intensity (magnitude squared) is a smooth function.
    # Thus, in order to allow a high-order spline, we take the square root of the interpolated square of the image.
    data = devlib.affine_transform(devlib.square(data), devlib.array(matrix), order=1, offset=offset)
    data = devlib.sqrt(devlib.clip(data, 0))
    if plot:
        arr = devlib.to_numpy(data)
        plt.imshow(arr[np.argmax(np.sum(arr, axis=(1, 2)))])
        plt.title(np.linalg.det(matrix))
        plt.show()

    if size is not None:
        data = pad_to_cube(data, size)
    return data


def pad_to_cube(data, size):
    shp = np.array([size, size, size]) // 2
    # Pad the array to the largest dimensions
    shp1 = np.array(data.shape) // 2
    pad = shp - shp1
    pad[pad < 0] = 0
    data = devlib.pad(data, [(pad[0], pad[0]), (pad[1], pad[1]), (pad[2], pad[2])])
    # Crop the array to the final dimensions
    shp1 = np.array(data.shape) // 2
    start, end = shp1 - shp, shp1 + shp
    data = data[start[0]:end[0], start[1]:end[1], start[2]:end[2]]
    return data


class Peak:
    """
    Holds parameters related to a peak.
    """

    def __init__(self, peak_dir):
        geometry = ut.read_config(f"{peak_dir}/geometry")
        self.hkl = geometry["peak_hkl"]
        self.norm = np.linalg.norm(self.hkl)
        print(f"Loading peak {self.hkl}")

        # Geometry parameters
        self.g_vec = devlib.array([0, * self.hkl]) * 2*pi/geometry["lattice"]
        self.gdotg = devlib.array(devlib.dot(self.g_vec, self.g_vec))
        self.size = geometry["final_size"]
        self.rs_lab_to_det = np.array(geometry["rmatrix"]) / geometry["rs_voxel_size"]
        self.rs_det_to_lab = np.linalg.inv(self.rs_lab_to_det)
        self.ds_lab_to_det = self.rs_det_to_lab.T
        self.ds_det_to_lab = self.rs_lab_to_det.T

        datafile = (Path(peak_dir) / "phasing_data/data.tif").as_posix()
        self.full_data = devlib.absolute(devlib.from_numpy(tf.imread(datafile)))
        self.full_data = devlib.moveaxis(self.full_data, 0, 2)
        self.full_data = devlib.moveaxis(self.full_data, 0, 1)
        self.full_size = np.max(self.full_data.shape)
        self.full_data = pad_to_cube(self.full_data, self.full_size)
        self.data = pad_to_cube(self.full_data, self.size)

        # Resample the diffraction patterns into the lab reference frame.
        self.res_data = resample(self.full_data, self.rs_det_to_lab, self.size)
        tf.imwrite((Path(peak_dir) / "phasing_data/data_resampled.tif").as_posix(), devlib.to_numpy(self.res_data))
        # self.res_mask = resample(devlib.array(np.ones(self.full_data.shape)), self.det_to_lab, self.size)

        # Do the fftshift on all the arrays
        self.full_data = devlib.fftshift(self.full_data)
        self.data = devlib.fftshift(self.data)
        self.res_data = devlib.fftshift(self.res_data)


class CoupledRec(Rec):
    """
    Performs a coupled reconstruction of multiple Bragg peaks using iterative phase retrieval. It alternates between a
    shared object with a density and atomic displacement field and a working object with an amplitude and phase. The
    general outline of this process is as follows:
    1. Initialize the shared object with random values.
    2. Randomly select a diffraction pattern and corresponding reciprocal lattice vector G from the collected data.
    3. Set the working object to the projection of the shared object onto G.
    4. Apply standard phase retrieval techniques to the working object for a set number of iterations.
    5. Update the G-projection of the shared object to be a weighted average of its current value with the working
        object.
    6. Repeat steps 2-5.

    params : dict
        parameters used in reconstruction. Refer to x for parameters description
    data_file : str
        name of file containing data for each peak to be reconstructed

    """
    __author__ = "Nick Porter"
    __all__ = []

    def __init__(self, params, peak_dirs, pkg):
        super().__init__(params, None, pkg)

        self.iter_functions = self.iter_functions + [self.switch_resampling_operation, self.switch_peaks_operation]

        self.params["switch_peak_trigger"] = self.params.get("switch_peak_trigger", [0, 5])
        self.params["mp_max_weight"] = self.params.get("mp_max_weight", 0.9)
        self.params["mp_taper"] = self.params.get("mp_taper", 0.75)
        self.params["calc_strain"] = self.params.get("calc_strain", True)

        self.peak_dirs = peak_dirs

    def init_dev(self, device_id):
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        if device_id != -1:
            self.dev = device_id
            if device_id != -1:
                try:
                    devlib.set_device(device_id)
                except Exception as e:
                    print(e)
                    print('may need to restart GUI')
                    return -1

        # Allows for a control peak to be left out of the reconstruction for comparison
        all_peaks = [Peak(d) for d in self.peak_dirs]
        if "control_peak" in self.params:
            control_peak = self.params["control_peak"]
            self.peak_objs = [p for p in all_peaks if p.hkl != control_peak]
            try:
                self.ctrl_peak = next(p for p in all_peaks if p.hkl == control_peak)
                self.ctrl_error = []
            except StopIteration:
                self.ctrl_peak = None
                self.ctrl_error = None
        else:
            self.peak_objs = all_peaks
            self.ctrl_peak = None
            self.ctrl_error = None

        # Fast resampling means using the resampled data to reconstruct the object in a single common basis
        # When this is set to False, the reconstruction will need to be resampled each time the peak is switched
        self.fast_resample = True
        self.lab_frame = True

        self.num_peaks = len(self.peak_objs)
        self.pk = 0  # index in list of current peak being reconstructed
        self.data = self.peak_objs[self.pk].res_data
        self.iter_data = self.data
        self.dims = self.data.shape

        if self.need_save_data:
            self.saved_data = devlib.copy(self.data)
            self.need_save_data = False

        return 0

    def init(self, img_dir=None, alpha_dir=None, gen=None):
        if super().init(img_dir, alpha_dir, gen) == -1:
            return -1

        # Define the shared image
        self.shared_image = devlib.absolute(self.ds_image[:, :, :, None]) * devlib.array([1, 1, 1, 1])
        self.rho_hat = devlib.array([1, 0, 0, 0])  # This is the density "unit vector" in the shared object.

        # Define the multipeak projection weighting and tapering
        coeff = self.params["mp_taper"] / (self.params["mp_taper"] - 1)
        self.proj_weight = devlib.square(devlib.cos(devlib.linspace(coeff*1.57, 1.57, self.iter_no).clip(0, 2)))
        self.proj_weight = self.proj_weight * self.params["mp_max_weight"]

        return 0

    def save_res(self, save_dir):
        from array import array
        # J = self.calc_jacobian()
        # s_xx = J[:,:,:,0,0][:,:,:,None]
        # s_yy = J[:,:,:,1,1][:,:,:,None]
        # s_zz = J[:,:,:,2,2][:,:,:,None]
        # s_xy = ((J[:,:,:,0,1] + J[:,:,:,1,0]) / 2)[:,:,:,None]
        # s_yz = ((J[:,:,:,1,2] + J[:,:,:,2,1]) / 2)[:,:,:,None]
        # s_zx = ((J[:,:,:,2,0] + J[:,:,:,0,2]) / 2)[:,:,:,None]
        #
        sup = self.support[:, :, :, None]
        self.shared_image = self.shared_image * sup
        if not os.path.exists(save_dir):
            os.makedirs(save_dir)

        if self.params["calc_strain"]:
            J = self.calc_jacobian()
            s_xx = J[:,:,:,0,0][:,:,:,None]
            s_yy = J[:,:,:,1,1][:,:,:,None]
            s_zz = J[:,:,:,2,2][:,:,:,None]
            s_xy = ((J[:,:,:,0,1] + J[:,:,:,1,0]) / 2)[:,:,:,None]
            s_yz = ((J[:,:,:,1,2] + J[:,:,:,2,1]) / 2)[:,:,:,None]
            s_zx = ((J[:,:,:,2,0] + J[:,:,:,0,2]) / 2)[:,:,:,None]
            final_rec = devlib.concatenate((self.shared_image, s_xx, s_yy, s_zz, s_xy, s_yz, s_zx, sup), axis=-1)
        else:
            s_00 = devlib.zeros(sup.shape)
            final_rec = devlib.concatenate((self.shared_image, s_00, s_00, s_00, s_00, s_00, s_00, sup), axis=-1)

        devlib.save(save_dir + "/reconstruction", final_rec)
        devlib.save(save_dir + "/image", self.shared_image)
        devlib.save(save_dir + "/shared_density", self.shared_image[:, :, :, 0])
        devlib.save(save_dir + "/shared_u1", self.shared_image[:, :, :, 1])
        devlib.save(save_dir + "/shared_u2", self.shared_image[:, :, :, 2])
        devlib.save(save_dir + "/shared_u3", self.shared_image[:, :, :, 3])
        devlib.save(save_dir + '/support', self.support)

        errs = array('f', self.errs)

        with open(ut.join(save_dir, 'errors.txt'), 'w+') as err_f:
            err_f.write('\n'.join(map(str, errs)))

        devlib.save(ut.join(save_dir, 'errors'), errs)

        metric = dvut.all_metrics(self.ds_image, self.errs)
        with open(ut.join(save_dir, 'metrics.txt'), 'w+') as f:
            f.write(str(metric))

        if self.ctrl_error is not None:
            ctrl_error = np.array(self.ctrl_error)
            np.save(save_dir + '/ctrl_error', ctrl_error)
            np.savetxt(save_dir + '/ctrl_error.txt', ctrl_error)
        return 0

    def get_phase_gradient(self):
        """Calculate the phase gradient for a given peak"""
        # Project onto the peak
        if self.fast_resample:
            self.iter_data = self.peak_objs[self.pk].res_data
        else:
            self.iter_data = self.peak_objs[self.pk].data

        self.to_working_image()

        # Iterate a few times to settle
        for _ in range(25):
            self.to_reciprocal_space()
            self.modulus()
            self.to_direct_space()
            self.er()

        # Calculate the continuous phase gradient using Hofmann's method
        # (https://doi.org/10.1103/PhysRevMaterials.4.013801)
        gradient = []
        voxel_size = self.params["ds_voxel_size"]
        dx0, dy0, dz0 = devlib.gradient(devlib.angle(self.ds_image), voxel_size)
        dx1, dy1, dz1 = devlib.gradient(devlib.angle(self.ds_image * devlib.exp(1j*pi/2)), voxel_size)
        dx2, dy2, dz2 = devlib.gradient(devlib.angle(self.ds_image * devlib.exp(-1j*pi/2)), voxel_size)
        for stack in ([dx0, dx1, dx2], [dy0, dy1, dy2], [dz0, dz1, dz2]):
            stack = devlib.array(stack)
            index = devlib.argmin(stack**2, axis=0)
            gradient.append(devlib.take_along_axis(stack, index[None, :, :, :], axis=0).squeeze(axis=0))

        return gradient

    def calc_jacobian(self):
        grad_phi = []
        for i, peak in enumerate(self.peak_objs):
            self.prev_pk = self.pk
            self.pk = i
            grad_phi.append(self.get_phase_gradient())
        grad_phi = devlib.array(grad_phi)
        G = devlib.array([peak.g_vec[1:] for peak in self.peak_objs])
        grad_phi = devlib.moveaxis(devlib.moveaxis(grad_phi, 0, -1), 0, -1)
        cond = devlib.where(self.support, True, False)
        final_shape = (grad_phi.shape[0], grad_phi.shape[1], grad_phi.shape[2], 3, 3)
        ind = devlib.moveaxis(devlib.indices(cond.shape), 0, -1).reshape((-1, 3))[cond.ravel()].get()
        jacobian = devlib.zeros(final_shape)
        print("Calculating strain...")
        for i, j, k in tqdm.tqdm(ind):
            jacobian[i, j, k] = devlib.lstsq(G, grad_phi[i, j, k])[0]
        return jacobian

    def switch_peaks_operation(self):
        self.to_shared_image()
        self.get_control_error()
        self.pk = random.choice([x for x in range(self.num_peaks) if x not in (self.pk,)])
        if self.fast_resample:
            self.iter_data = self.peak_objs[self.pk].res_data
        else:
            self.iter_data = self.peak_objs[self.pk].data
        self.to_working_image()

    def to_shared_image(self):
        if not self.fast_resample:
            if self.lab_frame:
                self.lab_frame = False
            else:
                self.shift_to_center()
                self.ds_image = resample(self.ds_image, self.peak_objs[self.pk].ds_det_to_lab, self.dims[0])
                self.support = resample(self.support, self.peak_objs[self.pk].ds_det_to_lab,
                                                    self.dims[0], plot=True)
                self.make_binary()

        beta = self.proj_weight[self.iter]
        pk = self.peak_objs[self.pk]
        old_image = (devlib.dot(self.shared_image, pk.g_vec) / pk.gdotg)[:, :, :, None] * pk.g_vec + \
                    devlib.dot(self.shared_image, self.rho_hat)[:, :, :, None] * self.rho_hat
        new_image = (devlib.angle(self.ds_image) / pk.gdotg)[:, :, :, None] * pk.g_vec + \
                    devlib.absolute(self.ds_image)[:, :, :, None] * self.rho_hat * pk.norm
        self.shared_image = self.shared_image + beta * (new_image - old_image)
        if self.er_iter:
            self.shared_image = self.shared_image * self.support[:, :, :, None]
            self.shift_to_center()

    def to_working_image(self):
        phi = devlib.dot(self.shared_image, self.peak_objs[self.pk].g_vec)
        rho = self.shared_image[:, :, :, 0] / self.peak_objs[self.pk].norm
        self.ds_image = rho * devlib.exp(1j * phi)
        if not self.fast_resample:
            self.ds_image = resample(self.ds_image, self.peak_objs[self.pk].ds_lab_to_det, self.dims[0])
            self.support = resample(self.support, self.peak_objs[self.pk].ds_lab_to_det,
                                                self.dims[0])
            self.make_binary()

    def get_control_error(self):
        if self.ctrl_peak is None:
            return
        phi = devlib.dot(self.shared_image, self.peak_objs[self.pk].g_vec)
        rho = self.shared_image[:, :, :, 0] / self.peak_objs[self.pk].norm
        img = devlib.absolute(devlib.ifft(rho * devlib.exp(1j * phi)))
        lin_hist = devlib.histogram2d(self.ctrl_peak.res_data, img, log=False)
        nmi = devlib.calc_nmi(lin_hist).get()
        ehd = devlib.calc_ehd(lin_hist).get()
        log_hist = devlib.histogram2d(self.ctrl_peak.res_data, img, log=True)
        lnmi = devlib.calc_nmi(log_hist).get()
        lehd = devlib.calc_ehd(log_hist).get()
        self.ctrl_error.append([nmi, lnmi, ehd, lehd])

    def progress_operation(self):
        ornt = self.peak_objs[self.pk].hkl
        prg = f'|  iter {self.iter:>4}  ' \
              f'|  [{ornt[0]:>2}, {ornt[1]:>2}, {ornt[2]:>2}]  ' \
              f'|  err {self.errs[-1]:0.6f}  ' \
              f'|  max {self.shared_image[:, :, :, 0].max():0.5f}  '
        if self.ctrl_error is not None:
            prg += f"|  NMI {self.ctrl_error[-1][0]:0.6f}  "
            prg += f"|  LNMI {self.ctrl_error[-1][1]:0.6f}  "
            prg += f"|  EHD {self.ctrl_error[-1][2]:0.6f}  "
            prg += f"|  LEHD {self.ctrl_error[-1][3]:0.6f}  "
        print(prg)

    def switch_resampling_operation(self):
        self.fast_resample = False

    def get_density(self):
        return self.shared_image[:, :, :, 0]

    def get_distortion(self):
        return self.shared_image[:, :, :, 1:].swapaxes(3, 2).swapaxes(2, 1).swapaxes(1, 0)

    def flip(self):
        self.shared_image = devlib.flip(self.shared_image, axis=(0, 1, 2))
        self.shared_image[:, :, :, 1:] *= -1
        self.support = devlib.flip(self.support, axis=(0, 1, 2))

    def shift_to_center(self):
        ind = devlib.center_of_mass(self.shared_image[:, :, :, 0])
        shift_dist = (self.dims[0]//2) - devlib.round(devlib.array(ind))
        shift_dist = devlib.to_numpy(shift_dist).tolist()
        self.shared_image = devlib.roll(self.shared_image, shift_dist, axis=(0, 1, 2))
        self.ds_image = devlib.roll(self.ds_image, shift_dist, axis=(0, 1, 2))
        self.support = devlib.roll(self.support, shift_dist, axis=(0, 1, 2))


def reconstruction(datafile, **kwargs):
    """
    Reconstructs the image from experiment data in datafile according to given parameters. The results: image.npy, support.npy, and errors.npy are saved in 'saved_dir' defined in kwargs, or if not defined, in the directory of datafile.

    example of the simplest kwargs parameters:
        - algorithm_sequence ='3*(20*ER+180*HIO)+20*ER'
        - shrink_wrap_trigger = [1, 1]
        - twin_trigger = [2]
        - progress_trigger = [0, 20]

    Parameters
    ----------
    datafile : str
        filename of phasing data. Must be either .tif format or .npy type

    kwargs : keyword arguments
        save_dir : str
            directory where results of reconstruction are saved as npy files. If not present, the reconstruction outcome will be save in the same directory where datafile is.
        processing : str
            the library used when running reconstruction. When the 'auto' option is selected the program will use the best performing library that is available, in the following order: cupy, numpy. The 'cp' option will utilize cupy, and 'np' will utilize numpy. Default is auto.
        device : list of int
            IDs of the target devices. If not defined, it will default to -1 for the OS to select device.
        algorithm_sequence : str
            Mandatory; example: "3* (20*ER + 180*HIO) + 20*ER". Defines algorithm applied in each iteration during modulus projection (ER or HIO) and during modulus (error correction or partial coherence correction). The "*" character means repeat, and the "+" means add to the sequence. The sequence may contain single brackets defining a group that will be repeated by the preceding multiplier. The alphabetic entries: 'ER', 'ERpc', 'HIO', 'HIOpc' define algorithms used in iterations.
            If the defined algorithm contains 'pc' then during modulus operation a partial coherence correction is applied,  but only if partial coherence (pc) feature is activated. If not activated, the phasing will use error correction instead.
        hio_beta : float
             multiplier used in hio algorithm
        twin_trigger : list
             example: [2]. Defines at which iteration to cut half of the array (i.e. multiply by 0s)
        twin_halves : list
            defines which half of the array is zeroed out in x and y dimensions. If 0, the first half in that dimension is zeroed out, otherwise, the second half.
        shrink_wrap_trigger : list
            example: [1, 1]. Defines when to update support array using the parameters below.
        shrink_wrap_type : str
            supporting "GAUSS" only. Defines which algorithm to use for shrink wrap.
        shrink_wrap_threshold : float
            only point with relative intensity greater than the threshold are selected
        shrink_wrap_gauss_sigma : float
            used to calculate the Gaussian filter
        initial_support_area : list
            If the values are fractional, the support area will be calculated by multiplying by the data array dimensions. The support will be set to 1s to this dimensions centered.
        phc_trigger : list
            defines when to update support array using the parameters below by applaying phase constrain.
        phc_phase_min : float
            point with phase below this value will be removed from support area
        phc_phase_max : float
            point with phase over this value will be removed from support area
        pc_interval : int
            defines iteration interval to update coherence.
        pc_type : str
            partial coherence algorithm. 'LUCY' type is supported.
        pc_LUCY_iterations : int
            number of iterations used in Lucy algorithm
        pc_LUCY_kernel : list
            coherence kernel area.
        lowpass_filter_trigger : list
            defines when to apply lowpass filter using the parameters below.
        lowpass_filter_sw_threshold : float
            used in Gass type shrink wrap when applying lowpass filter.
        lowpass_filter_range : list
            used when applying low resolution data filter while iterating. The values are linespaced for lowpass filter iterations from first value to last. The filter is gauss with sigma of linespaced value. If only one number given, the last value will default to 1.
        average_trigger : list
            defines when to apply averaging. Negative start means it is offset from the last iteration.
        progress_trigger : list of int
            defines when to print info on the console. The info includes current iteration and error.
        no_verify : boolean
            if in no_verify mode the verifier will not stop the progress, only print message
    """
    no_verify = kwargs.get('no_verify', False)
    error_msg = ver.verify('config_rec', kwargs)
    if len(error_msg) > 0:
        print(error_msg)
        if not no_verify:
            return

    if not os.path.isfile(datafile):
        print(f'no file found {datafile}')
        return

    if 'reconstructions' in kwargs and kwargs['reconstructions'] > 1:
        print('Use cohere_core-ui package to run multiple reconstructions. Processing single reconstruction.')
    device = kwargs.get('device', [-1])
    pkg = kwargs.get('processing', 'auto')
    if pkg == 'auto':
        if device == [-1] or sys.platform == 'darwin':
            pkg = 'np'
        else:
            try:
                import cupy as cp
                pkg = 'cp'
            except:
                try:
                    import torch
                    pkg = 'torch'
                except:
                    pkg = 'np'

    worker = Rec(kwargs, datafile, pkg)

    if worker.init_dev(device[0]) < 0:
        return

    continue_dir = kwargs.get('continue_dir')
    ret_code = worker.init(continue_dir)
    if ret_code < 0:
        return
    ret_code = worker.iterate()
    if ret_code < 0:
        return
    if 'save_dir' in kwargs:
        save_dir = kwargs['save_dir']
    else:
        save_dir, filename = os.path.split(datafile)
    if ret_code == 0:
        worker.save_res(save_dir)


class DataRec(Rec):
    """
    Class that performs phasing using iterative algorithm, but gets data
    directly (not through the data file).

    params : dict
        parameters used in reconstruction. Refer to x for parameters description
    data: data array
    pkg: library to be used for reconstruction
    progress_callback: method to be called in progress_operation()
    """
    def __init__(self, params, data, pkg, progress_callback=None):
        Rec.__init__(self, params, None, pkg)
        self.data = data
        self.progress_callback = progress_callback

    def init_dev(self, device_id):
        os.environ["CUDA_DEVICE_ORDER"] = "PCI_BUS_ID"
        if device_id != -1:
            self.dev = device_id
            if device_id != -1:
                try:
                    devlib.set_device(device_id)
                except Exception as e:
                    print(e)
                    print('may need to restart GUI')
                    return -1

        try:
            data = devlib.from_numpy(self.data)
        except Exception as e:
            print(e)
            return -1
        # in the formatted data the max is in the center, we want it in the corner, so do fft shift
        self.data = devlib.fftshift(devlib.absolute(data))
        self.dims = devlib.dims(self.data)
        print('data shape', self.dims)

        if self.need_save_data:
            self.saved_data = devlib.copy(self.data)
            self.need_save_data = False

        return 0


