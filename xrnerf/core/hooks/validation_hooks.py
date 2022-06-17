# @Author: zcy
# @Date:   2022-04-20 17:05:14
# @Last Modified by:   zcy
# @Last Modified time: 2022-06-15 17:03:30

import os

import imageio
import numpy as np
import torch
from mmcv.runner import get_dist_info
from mmcv.runner.hooks import HOOKS, Hook

from .utils import calculate_ssim, img2mse, mse2psnr, to8b


@HOOKS.register_module()
class SetValPipelineHook(Hook):
    """pass val dataset's pipeline to network."""
    def __init__(self, valset=None):
        self.val_pipeline = valset.pipeline

    def before_run(self, runner):  # only run once
        runner.model.module.set_val_pipeline(self.val_pipeline)
        del self.val_pipeline


@HOOKS.register_module()
class SaveSpiralHook(Hook):
    """save testset's render results with spiral poses 在每次val_step()之后调用
    用于保存test数据集的环型pose渲染图片 这些图片是没有groundtruth的 以视频方式保存."""
    def __init__(self, save_folder='validation'):
        self.save_folder = save_folder

    def after_val_iter(self, runner):
        rank, _ = get_dist_info()
        if rank == 0:
            cur_iter = runner.iter
            spiral_rgbs = np.stack(runner.outputs['spiral_rgbs'], 0)
            spiral_disps = np.stack(runner.outputs['spiral_disps'], 0)

            spiral_dir = os.path.join(runner.work_dir, self.save_folder)
            os.makedirs(spiral_dir, exist_ok=True)

            imageio.mimwrite(os.path.join(spiral_dir,
                                          '{}_rgb.mp4'.format(cur_iter)),
                             to8b(spiral_rgbs),
                             fps=30,
                             quality=8)
            imageio.mimwrite(os.path.join(spiral_dir,
                                          '{}_disp.mp4'.format(cur_iter)),
                             to8b(spiral_disps / np.max(spiral_disps)),
                             fps=30,
                             quality=8)


@HOOKS.register_module()
class ValidateHook(Hook):
    """在测试集上计算ssim psnr指标 保存图片."""
    def __init__(self, save_folder='validation'):
        self.save_folder = save_folder

    def after_val_iter(self, runner):
        rank, _ = get_dist_info()
        if rank == 0:
            cur_iter = runner.iter
            rgbs = runner.outputs['rgbs']
            disps = runner.outputs['disps']
            gt_imgs = runner.outputs['gt_imgs']
            if len(rgbs) == 0:
                return
            if rgbs[0].shape != gt_imgs[0].shape:
                return

            ########### calculate metrics ###########
            mse_list, psnr_list, ssim_list = [], [], []
            for i, rgb in enumerate(rgbs):
                gt_img = gt_imgs[i]
                if isinstance(gt_img, torch.Tensor):
                    gt_img = gt_img.cpu().numpy()

                mse = img2mse(rgb, gt_img)
                psnr = mse2psnr(mse)
                ssim = calculate_ssim(rgb,
                                      gt_img,
                                      data_range=gt_img.max() - gt_img.min(),
                                      multichannel=True)
                mse_list.append(mse.item())
                psnr_list.append(psnr.item())
                ssim_list.append(ssim)

            average_mse = sum(mse_list) / len(mse_list)
            average_psnr = sum(psnr_list) / len(psnr_list)
            average_ssim = sum(ssim_list) / len(ssim_list)
            ########### calculate metrics ###########

            ########### save test images ###########
            testset_dir = os.path.join(runner.work_dir, self.save_folder,
                                       str(cur_iter))
            os.makedirs(testset_dir, exist_ok=True)
            for i, rgb in enumerate(rgbs):
                filename = os.path.join(testset_dir, '{:03d}.png'.format(i))
                final_img, gt_img = rgb, gt_imgs[i]
                final_img = np.hstack((final_img, gt_img))
                imageio.imwrite(filename, to8b(final_img))
            ########### save test images ###########

            # metrics = {'test_mse':average_mse, 'test_psnr':average_psnr, 'test_ssim':average_ssim}
            # runner.log_buffer.update(metrics) # 不合适，没法做到每次val_step后输出当前值，他会跟之前的求一个滑动平均

            metrics = 'On testset, mse is {:.5f}, psnr is {:.5f}, ssim is {:.5f}'.format(
                average_mse, average_psnr, average_ssim)
            runner.logger.info(metrics)