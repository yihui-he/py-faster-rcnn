# --------------------------------------------------------
# Fast R-CNN
# Copyright (c) 2015 Microsoft
# Licensed under The MIT License [see LICENSE for details]
# Written by Ross Girshick
# --------------------------------------------------------

"""Test a Fast R-CNN network on an imdb (image database)."""

from fast_rcnn.config import cfg, get_output_dir
from fast_rcnn.bbox_transform import clip_boxes, bbox_transform_inv
import argparse
from utils.timer import Timer
import numpy as np
import cv2
import caffe
from fast_rcnn.nms_wrapper import nms
import cPickle
from utils.blob import im_list_to_blob
import os
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
import numpy as np

haskeys = True

def _get_image_blob(im):
    """Converts an image into a network input.

    Arguments:
        im (ndarray): a color image in BGR order

    Returns:
        blob (ndarray): a data blob holding an image pyramid
        im_scale_factors (list): list of image scales (relative to im) used
            in the image pyramid
    """
    im_orig = im.astype(np.float32, copy=True)
    im_orig -= cfg.PIXEL_MEANS

    im_shape = im_orig.shape
    im_size_min = np.min(im_shape[0:2])
    im_size_max = np.max(im_shape[0:2])

    processed_ims = []
    im_scale_factors = []

    for target_size in cfg.TEST.SCALES:
        im_scale = float(target_size) / float(im_size_min)
        # Prevent the biggest axis from being more than MAX_SIZE
        if np.round(im_scale * im_size_max) > cfg.TEST.MAX_SIZE:
            im_scale = float(cfg.TEST.MAX_SIZE) / float(im_size_max)
        im = cv2.resize(im_orig, None, None, fx=im_scale, fy=im_scale,
                        interpolation=cv2.INTER_LINEAR)
        im_scale_factors.append(im_scale)
        processed_ims.append(im)

    # Create a blob to hold the input images
    blob = im_list_to_blob(processed_ims)

    return blob, np.array(im_scale_factors)

def _get_rois_blob(im_rois, im_scale_factors):
    """Converts RoIs into network inputs.

    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        im_scale_factors (list): scale factors as returned by _get_image_blob

    Returns:
        blob (ndarray): R x 5 matrix of RoIs in the image pyramid
    """
    rois, levels = _project_im_rois(im_rois, im_scale_factors)
    rois_blob = np.hstack((levels, rois))
    return rois_blob.astype(np.float32, copy=False)

def _project_im_rois(im_rois, scales):
    """Project image RoIs into the image pyramid built by _get_image_blob.

    Arguments:
        im_rois (ndarray): R x 4 matrix of RoIs in original image coordinates
        scales (list): scale factors as returned by _get_image_blob

    Returns:
        rois (ndarray): R x 4 matrix of projected RoI coordinates
        levels (list): image pyramid levels used by each projected RoI
    """
    im_rois = im_rois.astype(np.float, copy=False)

    if len(scales) > 1:
        widths = im_rois[:, 2] - im_rois[:, 0] + 1
        heights = im_rois[:, 3] - im_rois[:, 1] + 1

        areas = widths * heights
        scaled_areas = areas[:, np.newaxis] * (scales[np.newaxis, :] ** 2)
        diff_areas = np.abs(scaled_areas - 224 * 224)
        levels = diff_areas.argmin(axis=1)[:, np.newaxis]
    else:
        levels = np.zeros((im_rois.shape[0], 1), dtype=np.int)

    rois = im_rois * scales[levels]

    return rois, levels

def _get_blobs(im, rois):
    """Convert an image and RoIs within that image into network inputs."""
    blobs = {'data' : None, 'rois' : None}
    blobs['data'], im_scale_factors = _get_image_blob(im)
    if not cfg.TEST.HAS_RPN:
        blobs['rois'] = _get_rois_blob(rois, im_scale_factors)
    return blobs, im_scale_factors

def im_detect(net, im, boxes=None):
    """Detect object classes in an image given object proposals.

    Arguments:
        net (caffe.Net): Fast R-CNN network to use
        im (ndarray): color image to test (in BGR order)
        boxes (ndarray): R x 4 array of object proposals or None (for RPN)

    Returns:
        scores (ndarray): R x K array of object class scores (K includes
            background as object category 0)
        boxes (ndarray): R x (4*K) array of predicted bounding boxes
    """
    blobs, im_scales = _get_blobs(im, boxes)
    # When mapping from image ROIs to feature map ROIs, there's some aliasing
    # (some distinct image ROIs get mapped to the same feature ROI).
    # Here, we identify duplicate feature ROIs, so we only compute features
    # on the unique subset.
    if cfg.DEDUP_BOXES > 0 and not cfg.TEST.HAS_RPN:
        v = np.array([1, 1e3, 1e6, 1e9, 1e12])
        hashes = np.round(blobs['rois'] * cfg.DEDUP_BOXES).dot(v)
        _, index, inv_index = np.unique(hashes, return_index=True,
                                        return_inverse=True)
        blobs['rois'] = blobs['rois'][index, :]
        boxes = boxes[index, :]

    if cfg.TEST.HAS_RPN:
        im_blob = blobs['data']
        blobs['im_info'] = np.array(
            [[im_blob.shape[2], im_blob.shape[3], im_scales[0]]],
            dtype=np.float32)

    # reshape network inputs
    net.blobs['data'].reshape(*(blobs['data'].shape))
    if cfg.TEST.HAS_RPN:
        net.blobs['im_info'].reshape(*(blobs['im_info'].shape))
    else:
        net.blobs['rois'].reshape(*(blobs['rois'].shape))

    # do forward
    forward_kwargs = {'data': blobs['data'].astype(np.float32, copy=False)}
    if cfg.TEST.HAS_RPN:
        forward_kwargs['im_info'] = blobs['im_info'].astype(np.float32, copy=False)
    else:
        forward_kwargs['rois'] = blobs['rois'].astype(np.float32, copy=False)
    blobs_out = net.forward(**forward_kwargs)

    if cfg.TEST.HAS_RPN:
        assert len(im_scales) == 1, "Only single-image batch implemented"
        rois = net.blobs['rois'].data.copy()
        # unscale back to raw image space
        boxes = rois[:, 1:5] / im_scales[0]

    if cfg.TEST.SVM:
        # use the raw scores before softmax under the assumption they
        # were trained as linear SVMs
        scores = net.blobs['cls_score'].data
    else:
        # use softmax estimated probabilities
        scores = blobs_out['cls_prob']

    if cfg.TEST.BBOX_REG:
        # Apply bounding-box regression deltas
        box_deltas = blobs_out['bbox_pred']
        pred_boxes = bbox_transform_inv(boxes, box_deltas)
        pred_boxes = clip_boxes(pred_boxes, im.shape)
    else:
        # Simply repeat the boxes, once for each class
        pred_boxes = np.tile(boxes, (1, scores.shape[1]))

    if cfg.DEDUP_BOXES > 0 and not cfg.TEST.HAS_RPN:
        # Map scores and predictions back to the original set of boxes
        scores = scores[inv_index, :]
        pred_boxes = pred_boxes[inv_index, :]


    # Apply bounding-box regression deltas
    if 'key_pred' in blobs_out:
        key_deltas = blobs_out['key_pred']
        pred_keys = bbox_transform_inv(boxes, key_deltas)
        pred_keys = clip_boxes(pred_keys, im.shape)
    
        return scores, pred_boxes, pred_keys

    return scores, pred_boxes

def vis_detections(im, class_name, dets, thresh=0.5, ax=None):
    """Visual debugging of detections."""
    savehuman=False

    im = im[:, :, (2, 1, 0)]
    if class_name == '2person':
        edgecolor = 'green'
        linewidth = 2
    else:
        linewidth = 1
        edgecolor='r'

    for i in xrange(np.minimum(10, dets.shape[0])):
        bbox = dets[i, :4]
        key = dets[i, 5:]
        score = dets[i, 4]
        if score > thresh:
            ax.add_patch(
            Rectangle((bbox[0], bbox[1]),
                        bbox[2] - bbox[0],
                        bbox[3] - bbox[1], fill=False,
                        edgecolor=edgecolor, linewidth=linewidth)
            )
            if class_name == 'person':
                savehuman=True
                ax.add_patch(
                Rectangle((key[0], key[1]),
                            key[2] - key[0],
                            key[3] - key[1], fill=False,
                            edgecolor='yellow', linewidth=linewidth)
                )
            ax.text(bbox[0] + 3, bbox[1]+7,
                    '{:s} {:.3f}'.format(class_name, score), color='green'# bbox=dict(facecolor='blue', alpha=0.5),
            )
    return savehuman

def apply_nms(all_boxes, thresh):
    """Apply non-maximum suppression to all predicted boxes output by the
    test_net method.
    """
    num_classes = len(all_boxes)
    num_images = len(all_boxes[0])
    nms_boxes = [[[] for _ in xrange(num_images)]
                 for _ in xrange(num_classes)]
    for cls_ind in xrange(num_classes):
        for im_ind in xrange(num_images):
            dets = all_boxes[cls_ind][im_ind]
            if dets == []:
                continue
            # CPU NMS is much faster than GPU NMS when the number of boxes
            # is relative small (e.g., < 10k)
            # TODO(rbg): autotune NMS dispatch
            keep = nms(dets, thresh, force_cpu=True)
            if len(keep) == 0:
                continue
            nms_boxes[cls_ind][im_ind] = dets[keep, :].copy()
    return nms_boxes

def test_net(net, imdb, max_per_image=100, thresh=0.3, vis=False, modelname=None, imdbname=''):
    """Test a Fast R-CNN network on an image database."""
    if modelname is None:
        modelname = 'images'
    else:
        modelname = os.path.splitext(os.path.basename(modelname))[0]

    # for key in ['bbox_pred', 'key_pred']:
    #     # scale and shift with bbox reg unnormalization; then save snapshot
    #     net.params[key][0].data[...] = \
    #             (net.params[key][0].data /
    #             0.1)
    #     net.params[key][1].data[...] = \
    #             net.params[key][1].data / 0.1

    if vis == True:
        savepath='output/im_'
        # if shownms:
        #     savepath+='nms_'
        savepath+=modelname+imdbname
        if not os.path.exists(savepath):
            os.mkdir(savepath)

    num_images = len(imdb.image_index)
    # all detections are collected into:
    #    all_boxes[cls][image] = N x 5 array of detections in
    #    (x1, y1, x2, y2, score)
    all_boxes = [[[] for _ in xrange(num_images)]
                 for _ in xrange(imdb.num_classes)]

    output_dir = get_output_dir(imdb, net)

    # timers
    _t = {'im_detect' : Timer(), 'misc' : Timer()}

    # if not cfg.TEST.HAS_RPN:
    #     roidb = imdb.roidb

    roidb = imdb.gt_roidb()

    for i in xrange(num_images):
        # i = 13
        im = cv2.imread(imdb.image_path_at(i))
        if vis:
            fig = plt.figure()
            ax = fig.add_subplot(111)
            ax.imshow(im[:, :, (2, 1, 0)])  
        # filter out any ground truth boxes
        if cfg.TEST.HAS_RPN:
            box_proposals = None

            _, im_scales = _get_blobs(im, box_proposals)

            # gt boxes: (x1, y1, x2, y2, cls)
            gt_inds = np.where(roidb[i]['gt_classes'] != 0)[0]
            gt_boxes = np.empty((len(gt_inds), 5), dtype=np.float32)
            gt_boxes[:, 0:4] = roidb[i]['boxes'][gt_inds, :] # * im_scales[0]
            gt_boxes[:, 4] = roidb[i]['gt_classes'][gt_inds]

            if vis:
                for gt_box in gt_boxes:
                    print gt_box
                    ax.add_patch(
                        Rectangle((gt_box[0], gt_box[1]),
                                gt_box[2] - gt_box[0],
                                gt_box[3] - gt_box[1], fill=False, linewidth=1)
                    )
                    # ax.add_patch(
                    #         Rectangle((gt_box[0+5], gt_box[1+5]),
                    #                 gt_box[2+5] - gt_box[0+5],
                    #                 gt_box[3+5] - gt_box[1+5], fill=False, linewidth=1)
                    #     )
                
                
        else:
            # The roidb may contain ground-truth rois (for example, if the roidb
            # comes from the training or val split). We only want to evaluate
            # detection on the *non*-ground-truth rois. We select those the rois
            # that have the gt_classes field set to 0, which means there's no
            # ground truth.
            box_proposals = roidb[i]['boxes'][roidb[i]['gt_classes'] == 0]

        _t['im_detect'].tic()
        if not haskeys:
            scores, boxes = im_detect(net, im, box_proposals)
        else:
            scores, boxes, keys = im_detect(net, im, box_proposals)
        _t['im_detect'].toc()

        _t['misc'].tic()
           
        # skip j = 0, because it's the background class
        savehuman = False
        for j in xrange(1, imdb.num_classes):
            inds = np.where(scores[:, j] > thresh)[0]
            cls_scores = scores[inds, j]
            print j,cls_scores
            cls_boxes = boxes[inds, j*4:(j+1)*4]
            cls_dets = np.hstack((cls_boxes, cls_scores[:, np.newaxis])) \
                .astype(np.float32, copy=False)
            keep = nms(cls_dets, cfg.TEST.NMS)
            cls_dets = cls_dets[keep, :]
            if haskeys:
                cls_keys = keys[inds, j*4:(j+1)*4]
                cls_dets = np.hstack((cls_dets, cls_keys[keep, :])) \
                    .astype(np.float32, copy=False)
            if vis:
                if vis_detections(im, imdb.classes[j], cls_dets, ax=ax):
                    savehuman = True
            all_boxes[j][i] = cls_dets[:, :5]
            # all_boxes[j][i] = cls_dets

        if vis:
            ax.axis((0,im.shape[1],im.shape[0], 0))
            ax.tick_params(
                axis='both',          # changes apply to the x-axis
                which='both',      # both major and minor ticks are affected
                bottom='off',      # ticks along the bottom edge are off
                top='off',         # ticks along the top edge are off
                left='off',      # ticks along the bottom edge are off
                right='off',         # ticks along the top edge are off
                labelleft='off',
                labelright='off',
                labeltop='off',
                labelbottom='off') # labels along the bottom edge are off
            save_dir = savepath+'/'+str(i)+'.png'
            # plt.title(save_dir)
            
            if savehuman:
                plt.savefig(save_dir, bbox_inches='tight')
            # plt.show()
            plt.close()

        # Limit to max_per_image detections *over all classes*
        if max_per_image > 0:
            image_scores = np.hstack([all_boxes[j][i][:, -1]
                                      for j in xrange(1, imdb.num_classes)])
            if len(image_scores) > max_per_image:
                image_thresh = np.sort(image_scores)[-max_per_image]
                for j in xrange(1, imdb.num_classes):
                    keep = np.where(all_boxes[j][i][:, -1] >= image_thresh)[0]
                    all_boxes[j][i] = all_boxes[j][i][keep, :]
        _t['misc'].toc()

        print 'im_detect: {:d}/{:d} {:.3f}s {:.3f}s' \
              .format(i + 1, num_images, _t['im_detect'].average_time,
                      _t['misc'].average_time)

    det_file = os.path.join(output_dir, 'detections.pkl')
    with open(det_file, 'wb') as f:
        cPickle.dump(all_boxes, f, cPickle.HIGHEST_PROTOCOL)

    print 'Evaluating detections'
    imdb.evaluate_detections(all_boxes, output_dir)
