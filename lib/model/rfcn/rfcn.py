# --------------------------------------------------------
# Pytorch Faster R-CNN
# Licensed under The MIT License [see LICENSE for details]
# Written by Aries Zhang
# --------------------------------------------------------
import random
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torchvision.models as models
from torch.autograd import Variable
import numpy as np
from model.utils.config import cfg
from model.rpn.rpn import _RPN
from model.psroi_pooling.modules.psroi_pool import PSRoIPool
from model.rpn.proposal_target_layer_cascade import _ProposalTargetLayer
import time
import pdb
from model.utils.net_utils import _smooth_l1_loss, _crop_pool_layer, _affine_grid_gen, _affine_theta
from model.utils.loss import detect_loss, ohem_detect_loss

class _RFCN(nn.Module):
    """ RFCN """
    def __init__(self, classes, class_agnostic):
        super(_RFCN, self).__init__()
        self.classes = classes
        self.n_classes = len(classes)
        self.class_agnostic = class_agnostic

        self.box_num_classes = 1 if class_agnostic else self.n_classes

        # define rpn
        self.RCNN_rpn = _RPN(self.dout_base_model)
        self.RCNN_proposal_target = _ProposalTargetLayer(self.n_classes)

        # loss
        self.RCNN_loss_cls = 0
        self.RCNN_loss_bbox = 0
        
        self.RCNN_conv_1x1 = nn.Conv2d(in_channels=2048, out_channels=1024,
                  kernel_size=1, stride=1, padding=0, bias=False)

        if self.class_agnostic:
            self.RCNN_bbox_base = nn.Conv2d(in_channels=1024, out_channels=4 * cfg.POOLING_SIZE * cfg.POOLING_SIZE,
                                            kernel_size=1, stride=1, padding=0, bias=False)
        else:
            self.RCNN_bbox_base = nn.Conv2d(in_channels=1024, out_channels=4 * self.n_classes * cfg.POOLING_SIZE * cfg.POOLING_SIZE,
                                            kernel_size=1, stride=1, padding=0, bias=False)

        self.RCNN_cls_base = nn.Conv2d(in_channels=1024, out_channels=self.n_classes * cfg.POOLING_SIZE * cfg.POOLING_SIZE,
                                       kernel_size=1, stride=1, padding=0, bias=False)

        self.RCNN_psroi_pool_cls = PSRoIPool(cfg.POOLING_SIZE, cfg.POOLING_SIZE,
                                          spatial_scale=1/16.0, group_size=cfg.POOLING_SIZE,
                                          output_dim=self.n_classes)
        self.RCNN_psroi_pool_loc = PSRoIPool(cfg.POOLING_SIZE, cfg.POOLING_SIZE,
                                          spatial_scale=1/16.0, group_size=cfg.POOLING_SIZE,
                                          output_dim=self.box_num_classes * 4)
        self.pooling = nn.AvgPool2d(kernel_size=cfg.POOLING_SIZE, stride=cfg.POOLING_SIZE)
        self.grid_size = cfg.POOLING_SIZE * 2 if cfg.CROP_RESIZE_WITH_MAX_POOL else cfg.POOLING_SIZE

    def forward(self, im_data, im_info, gt_boxes, num_boxes):
        batch_size = im_data.size(0)

        im_info = im_info.data
        gt_boxes = gt_boxes.data
        num_boxes = num_boxes.data

        # feed image data to base model to obtain base feature map
        base_feat = self.RCNN_base(im_data)
        
        # feed base feature map tp RPN to obtain rois
        rois, rpn_loss_cls, rpn_loss_bbox = self.RCNN_rpn(base_feat, im_info, gt_boxes, num_boxes)

        # if it is training phrase, then use ground trubut bboxes for refining
        if self.training:
            roi_data = self.RCNN_proposal_target(rois, gt_boxes, num_boxes)
            rois, rois_label, rois_target, rois_inside_ws, rois_outside_ws = roi_data

            rois_label = Variable(rois_label.view(-1).long())
            rois_target = Variable(rois_target.view(-1, rois_target.size(2)))
            rois_inside_ws = Variable(rois_inside_ws.view(-1, rois_inside_ws.size(2)))
            rois_outside_ws = Variable(rois_outside_ws.view(-1, rois_outside_ws.size(2)))
        else:
            rois_label = None
            rois_target = None
            rois_inside_ws = None
            rois_outside_ws = None
            rpn_loss_cls = 0
            rpn_loss_bbox = 0

        # prepare for ps pooling
        rois = Variable(rois)
        base_feat = self.RCNN_conv_remain(base_feat)
        base_feat = F.relu(self.RCNN_conv_1x1(base_feat))

        # do roi pooling based on predicted rois
        cls_feat = self.RCNN_cls_base(base_feat)
        pooled_feat_cls = self.RCNN_psroi_pool_cls(cls_feat, rois.view(-1, 5))
        cls_score = self.pooling(pooled_feat_cls)
        cls_score = cls_score.squeeze()

        bbox_base = self.RCNN_bbox_base(base_feat)
        pooled_feat_loc = self.RCNN_psroi_pool_loc(bbox_base, rois.view(-1, 5))
        pooled_feat_loc = self.pooling(pooled_feat_loc)
        bbox_pred = pooled_feat_loc.squeeze()

        if self.training and not self.class_agnostic:
            # select the corresponding columns according to roi labels
            bbox_pred_view = bbox_pred.view(bbox_pred.size(0), int(bbox_pred.size(1) / 4), 4)
            bbox_pred_select = torch.gather(bbox_pred_view, 1, rois_label.view(rois_label.size(0), 1, 1).expand(rois_label.size(0), 1, 4))
            bbox_pred = bbox_pred_select.squeeze(1)

        # compute object classification probability
        cls_prob = F.softmax(cls_score, dim=1)

        RCNN_loss_cls = 0
        RCNN_loss_bbox = 0

        if self.training:
            loss_func = ohem_detect_loss if cfg.TRAIN.OHEM else detect_loss
            RCNN_loss_cls, RCNN_loss_bbox = loss_func(batch_size, cls_score, rois_label, bbox_pred, rois_target, rois_inside_ws, rois_outside_ws)


        cls_prob = cls_prob.view(batch_size, rois.size(1), -1)
        bbox_pred = bbox_pred.view(batch_size, rois.size(1), -1)

        return rois, cls_prob, bbox_pred, rpn_loss_cls, rpn_loss_bbox, RCNN_loss_cls, RCNN_loss_bbox, rois_label
        
    def _init_weights(self):
        def normal_init(m, mean, stddev, truncated=False):
            """
            weight initalizer: truncated normal and random normal.
            """
            # x is a parameter
            if truncated:
                m.weight.data.normal_().fmod_(2).mul_(stddev).add_(mean) # not a perfect approximation
            else:
                m.weight.data.normal_(mean, stddev)
                if m.bias is not None:
                    m.bias.data.zero_()

        normal_init(self.RCNN_rpn.RPN_Conv, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_rpn.RPN_cls_score, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_rpn.RPN_bbox_pred, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_conv_1x1, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_cls_base, 0, 0.01, cfg.TRAIN.TRUNCATED)
        normal_init(self.RCNN_bbox_base, 0, 0.001, cfg.TRAIN.TRUNCATED)

    def create_architecture(self):
        self._init_modules()
        self._init_weights()