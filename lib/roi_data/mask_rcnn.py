# Copyright (c) 2017-present, Facebook, Inc.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
##############################################################################
"""Construct minibatches for Mask R-CNN training. Handles the minibatch blobs
that are specific to Mask R-CNN. Other blobs that are generic to RPN or
Fast/er R-CNN are handled by their respecitive roi_data modules.
"""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function
from __future__ import unicode_literals

import logging
import numpy as np

from core.config import cfg
import utils.blob as blob_utils
import utils.boxes as box_utils
import utils.segms as segm_utils


def add_mask_rcnn_blobs(blobs, sampled_boxes, roidb, im_scale, batch_idx):
    """Add Mask R-CNN specific blobs to the input blob dictionary."""
    # Prepare the mask targets by associating one gt mask to each training roi
    # that has a fg (non-bg) class label.
    M = cfg.MRCNN.RESOLUTION
    rle_gt_inds = np.where((roidb['gt_classes'] > 0) & (roidb['is_crowd'] == 0))[0]
    rles_gt = [roidb['segms'][i] for i in rle_gt_inds]
    #boxes_from_polys = segm_utils.rle_masks_to_boxes(polys_gt)  # TODO: DI WU comment this line and uncomment next line
    boxes_from_rles = np.array([roidb['boxes'][i] for i in rle_gt_inds])
    fg_inds = np.where(blobs['labels_int32'] > 0)[0]
    roi_has_mask = blobs['labels_int32'].copy()
    roi_has_mask[roi_has_mask > 0] = 1

    if fg_inds.shape[0] > 0:
        # Class labels for the foreground rois
        mask_class_labels = blobs['labels_int32'][fg_inds]
        masks = blob_utils.zeros((fg_inds.shape[0], M**2), int32=True)

        # Find overlap between all foreground rois and the bounding boxes
        # enclosing each segmentation
        rois_fg = sampled_boxes[fg_inds]
        overlaps_bbfg_bbpolys = box_utils.bbox_overlaps(
            rois_fg.astype(np.float32, copy=False),
            boxes_from_rles.astype(np.float32, copy=False))
        # Map from each fg rois to the index of the mask with highest overlap
        # (measured by bbox overlap)
        fg_polys_inds = np.argmax(overlaps_bbfg_bbpolys, axis=1)

        # add fg targets
        for i in range(rois_fg.shape[0]):
            fg_polys_ind = fg_polys_inds[i]
            rle_gt = rles_gt[fg_polys_ind]
            roi_fg = rois_fg[i]
            # Rasterize the portion of the polygon mask within the given fg roi
            # to an M x M binary image
            mask = segm_utils.rle_to_mask_wrt_box(rle_gt, roi_fg, M)
            #mask = segm_utils.polys_to_mask_wrt_box(poly_gt, roi_fg, M)
            mask = np.array(mask > 0, dtype=np.int32)  # Ensure it's binary
            masks[i, :] = np.reshape(mask, M**2)
    else:  # If there are no fg masks (it does happen)
        # The network cannot handle empty blobs, so we must provide a mask
        # We simply take the first bg roi, given it an all -1's mask (ignore
        # label), and label it with class zero (bg).
        bg_inds = np.where(blobs['labels_int32'] == 0)[0]
        # rois_fg is actually one background roi, but that's ok because ...
        rois_fg = sampled_boxes[bg_inds[0]].reshape((1, -1))
        # We give it an -1's blob (ignore label)
        masks = -blob_utils.ones((1, M**2), int32=True)
        # We label it with class = 0 (background)
        mask_class_labels = blob_utils.zeros((1, ))
        # Mark that the first roi has a mask
        roi_has_mask[0] = 1

    if cfg.MRCNN.CLS_SPECIFIC_MASK:
        masks = _expand_to_class_specific_mask_targets(masks,
                                                       mask_class_labels)

    # Scale rois_fg and format as (batch_idx, x1, y1, x2, y2)
    rois_fg *= im_scale
    repeated_batch_idx = batch_idx * blob_utils.ones((rois_fg.shape[0], 1))
    rois_fg = np.hstack((repeated_batch_idx, rois_fg))

    # Update blobs dict with Mask R-CNN blobs
    blobs['mask_rois'] = rois_fg
    blobs['roi_has_mask_int32'] = roi_has_mask
    blobs['masks_int32'] = masks


def _expand_to_class_specific_mask_targets(masks, mask_class_labels):
    """Expand masks from shape (#masks, M ** 2) to (#masks, #classes * M ** 2)
    to encode class specific mask targets.
    """
    assert masks.shape[0] == mask_class_labels.shape[0]
    M = cfg.MRCNN.RESOLUTION

    # Target values of -1 are "don't care" / ignore labels
    mask_targets = -blob_utils.ones(
        (masks.shape[0], cfg.MODEL.NUM_CLASSES * M**2), int32=True)

    for i in range(masks.shape[0]):
        cls = int(mask_class_labels[i])
        start = M**2 * cls
        end = start + M**2
        # Ignore background instance
        # (only happens when there is no fg samples in an image)
        if cls > 0:
            mask_targets[i, start:end] = masks[i, :]

    return mask_targets


def add_car_cls_rcnn_blobs(blobs, roidb, fg_inds, sampled_labels):
    """Add Car classification specific blobs to the input blob dictionary."""

    labels_car_cls = np.ones(sampled_labels.shape) * (-1)
    quaternions = np.ones((sampled_labels.shape[0], 4)) * (-100)

    for i, ind in enumerate(fg_inds):
        ind_temp = roidb['box_to_gt_ind_map'][ind]
        if ind_temp == -1:
            raise AssertionError("This is a background class")
        else:
            labels_car_cls[i] = roidb['car_cat_classes'][ind_temp]
            quaternions[i] = roidb['quaternions'][ind_temp]

    blobs['car_cls_labels_int32'] = labels_car_cls
    blobs['quaternions'] = quaternions


def add_car_trans_rcnn_blobs(blobs, roidb, fg_inds, sampled_labels):
    """Add Car classification specific blobs to the input blob dictionary."""

    def normalise_pose(trans):
        trans -= np.array(cfg.TRANS_HEAD.TRANS_MEAN)
        trans /= np.array(cfg.TRANS_HEAD.TRANS_STD)
        return trans

    car_trans = np.ones((sampled_labels.shape[0], 3)) * (-100)

    for i, ind in enumerate(fg_inds):
        ind_temp = roidb['box_to_gt_ind_map'][ind]
        if ind_temp == -1:
            raise AssertionError("This is a background class")
        else:
            poses = roidb['poses'][ind_temp]
            car_trans[i] = normalise_pose(poses[3:])

    blobs['car_trans'] = car_trans