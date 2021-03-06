# external library & framework import (TODO remove unnecessary ones)
# TODO could updateFrame be run asynchronously ? That way we wouldn't need to wait for it
import numpy as np
import os
import sys
import time
import argparse
import json
from PIL import Image
import matplotlib.pyplot as plt

# Torch imports
import torch
import torch.utils.data as data
import torch.optim as optim
from torch.autograd import Variable

# Using OpenCV to save images
import cv2

import asyncio

# Local file imports
sys.path.insert(0,'../modules')
from sample_generator import *
from data_prov import *
from model import *
from bbreg import *
from options import *
from gen_config import *



# Own
from TrackerUtils import *

cv2.namedWindow("image", cv2.WINDOW_NORMAL)



np.random.seed(123)
torch.manual_seed(456)

if opts["use_gpu"]:
    torch.cuda.manual_seed(789)

DEFAULT_RESULT_LENGTH=20 #Default length of the result and result_bb arrays
# STARTING POINT VARIABLES
# Using the starting point to make a rectangle out of the first bbox
START_POINT_HEIGHT =  3 # Height of the starting point rectangle
START_POINT_WIDTH = 3 # Width of the starting point rectangle



# FILE VARIABLES
VIDEO_SRC = "../trafficvid1.mp4"
YOLO_OUTPUT_DIR = "../yolo_output"
ORIGINAL_FRAME_JPG_NAME = "OG_FRAME.jpg"


# TODO important: del reference after tracker reached target or was in emergency mode too long
class Tracker:
    coordinates={}
    coordinates["start"]={} # For storing start point
    coordinates["start"]["x1"]=-1
    coordinates["start"]["x2"]=-1
    coordinates["start"]["y1"]=-1
    coordinates["start"]["y2"]=-1 # Preventing KeyError on initialization, but making the check easy:)
    coordinates["current"]={} # For storing current coordinates (at n'th frame)
    coordinates["history"]=[] # For storing the history / all the coordinates that have been set at some point



    # Constructor
    def __init__(self):
        # Declaring Variables that contain class specific content (non-static)
        self.model = None
        self.pos_feats_all = None
        self.neg_feats_all = None
        self.sample_generator = None
        self.pos_generator = None
        self.neg_generator = None
        self.bbreg = None
        self.target_bbox = None
        self.bbreg_bbox = None
        self.result = None
        self.result_bb = None
        self.feat_dim = None
        self.criterion = None
        self.update_optimizer = None



        # EMERGENCY MODE VARIABLES
        # Using the emergency mode to reduce the threshold so that we are able to track the object through "full" occlusions (snapping back onto it after it leaves the occlusion)
        # This paramater may need some fine-tuning depending on the video
        self.EMERGENCY_MODE = False
        self.EMERGENCY_MODE_THRESHOLD = -2
        self.EMERGENCY_MODE_WAIT_FRAMES = 50
        self.EMERGENCY_MODE_FRAMES_COUNTER=0


        self.frameNumber = 0

    # Initiates the tracker with either coordinates["start"] or given parameters
    #, x1=coordinates["start"]["x1"], y1=coordinates["start"]["y1"], x2=coordinates["start"]["x2"], y2=coordinates["start"]["y2"]
    def startTracking(self, frame, init_bbox):
        print("Running startTracking()")
        # Init bbox
        self.target_bbox = np.array(init_bbox)
        self.result = np.zeros((DEFAULT_RESULT_LENGTH,4))
        self.result_bb = np.zeros((DEFAULT_RESULT_LENGTH,4))


        self.result[0] = self.target_bbox
        self.result_bb[0] = self.target_bbox

        # Init model
        self.model = MDNet(opts['model_path'])
        if opts['use_gpu']:
            self.model = self.model.cuda()
        else:
            print("Not using CUDA")
        self.model.set_learnable_params(opts['ft_layers'])


        # Init criterion and optimizer
        self.criterion = BinaryLoss()
        init_optimizer = set_optimizer(self.model, opts['lr_init'])
        self.update_optimizer = set_optimizer(self.model, opts['lr_update'])

        tic = time.time()
        # Load first image
        print("Loading image...")
        image = Image.open(frame).convert('RGB')
        print("Successfully loaded image.")

        # Train bbox regressor
        bbreg_examples = gen_samples(SampleGenerator('uniform', image.size, 0.3, 1.5, 1.1),
                                    self.target_bbox, opts['n_bbreg'], opts['overlap_bbreg'], opts['scale_bbreg'])
        bbreg_feats = forward_samples(self.model, image, bbreg_examples)
        self.bbreg = BBRegressor(image.size)
        self.bbreg.train(bbreg_feats, bbreg_examples, self.target_bbox)

        # Getting positive examples
        pos_examples = gen_samples(SampleGenerator('gaussian', image.size, 0.1, 1.2),
                                   self.target_bbox, opts['n_pos_init'], opts['overlap_pos_init'])
        print("Generated positive examples.")


        # Getting negative image examples
        neg_examples = np.concatenate([
                        gen_samples(SampleGenerator('uniform', image.size, 1, 2, 1.1),
                                    self.target_bbox, opts['n_neg_init']//2, opts['overlap_neg_init']),
                        gen_samples(SampleGenerator('whole', image.size, 0, 1.2, 1.1),
                                    self.target_bbox, opts['n_neg_init']//2, opts['overlap_neg_init'])])

        neg_examples = np.random.permutation(neg_examples)
        print("Generated negative examples.")

        # Extract pos/neg features
        pos_feats = forward_samples(self.model, image, pos_examples)
        neg_feats = forward_samples(self.model, image, neg_examples)
        self.feat_dim = pos_feats.size(-1)
        print("Extracted positive & negative features from examples.")


        # pos_feats/neg_feats contain the features that the convnet should look out for!
        print("Started model training.")
        # Initial training
        train(self.model, self.criterion, init_optimizer, pos_feats, neg_feats, opts['maxiter_init']) #The model gets trained to watch those features
        print("Finished model training.")

        # Init sample generators
        self.sample_generator = SampleGenerator('gaussian', image.size, opts['trans_f'], opts['scale_f'], valid=True)
        self.pos_generator = SampleGenerator('gaussian', image.size, 0.1, 1.2)
        self.neg_generator = SampleGenerator('uniform', image.size, 1.5, 1.2)
        print("Initiated SampleGenerators")

        # Init pos/neg features for update
        self.pos_feats_all = [pos_feats[:opts['n_pos_update']]]
        self.neg_feats_all = [neg_feats[:opts['n_neg_update']]]

        return True #self.result and self.result_bbox are already saved in this class, no reason to return them then


    # Updates the frame, runs the network over it, returns the location (if there is any)
    def updateFrame(self, frame, frameNumber):
        # Load image
        image = Image.open(frame).convert('RGB')
        self.frameNumber = frameNumber

        # Estimate target bbox
        samples = gen_samples(self.sample_generator, self.target_bbox, opts['n_samples'])
        sample_scores = forward_samples(self.model, image, samples, out_layer='fc6')
        top_scores, top_idx = sample_scores[:,1].topk(5)
        top_idx = top_idx.cpu().numpy()
        target_score = top_scores.mean()
        self.target_bbox = samples[top_idx].mean(axis=0)

        #print(self.target_bbox)



        success = None
        # Enabling / Disabling EMERGENCY_MODE
        if target_score < opts['success_thr']:
            # Seems like we lost the object, set the emergency threshold and wait for the defined amount of frames
            self.EMERGENCY_MODE = True
            print("Enabled emergency mode, lowering threshold to: " + str(self.EMERGENCY_MODE_THRESHOLD))
            if self.EMERGENCY_MODE_FRAMES_COUNTER == self.EMERGENCY_MODE_WAIT_FRAMES:
                self.result[frameNumber] = self.target_bbox
                #the frame counter reached the end
                # TODO kill this tracker
                exit()
            self.EMERGENCY_MODE_FRAMES_COUNTER += 1 #increasing the frame counter

            # setting success variable so that we actually run the code we want to run in emergency mode
            success = target_score > self.EMERGENCY_MODE_THRESHOLD
            pass
        else:
            if self.EMERGENCY_MODE:
                print("Disabled emergency mode, setting threshold back to: " + str(opts['success_thr'])) #printing disable message
            self.EMERGENCY_MODE = False #disabling emergency mode
            # Doing the normal threshold comparison since this is the non-emergency mode
            success = target_score > opts['success_thr']

        print(target_score)
        print("Success:", success)
        # Expand search area at failure
        if success:
            self.sample_generator.set_trans_f(opts['trans_f']) #Everything fine, use normal search area
        else:
            self.sample_generator.set_trans_f(opts['trans_f_expand']) #If success=False, expand the search

        # REMARK: Area expansion and the emergency mode are (tested with one vid so far) able to get back on track with a fully occluded object

        # Bbox regression
        if success:
            bbreg_samples = samples[top_idx]
            bbreg_feats = forward_samples(self.model, image, bbreg_samples)
            bbreg_samples = self.bbreg.predict(bbreg_feats, bbreg_samples)
            self.bbreg_bbox = bbreg_samples.mean(axis=0)
        else:
            self.bbreg_bbox = self.target_bbox

        # Copy previous result at failure
        if not success:
            self.target_bbox = self.result[frameNumber-1]
            self.bbreg_bbox = self.result_bb[frameNumber-1]

        if frameNumber >= len(self.result):
            # Increase the length of self.result and self.result_bb when the maxmimum has been hit
            self.result = np.append(self.result,[[0,0,0,0]], axis=0) #adding a new row as soon as we hit the end, that way we have place for the current result
            self.result_bb = np.append(self.result_bb,[[0,0,0,0]], axis=0)

        # Save result
        self.result_bb[frameNumber] = self.bbreg_bbox

        # Data collectd
        print("Collecting data")
        if success:
            # Draw pos/neg samples
            pos_examples = gen_samples(self.pos_generator, self.target_bbox,
                                       opts['n_pos_update'],
                                       opts['overlap_pos_update'])
            neg_examples = gen_samples(self.neg_generator, self.target_bbox,
                                       opts['n_neg_update'],
                                       opts['overlap_neg_update'])

            # Extract pos/neg features
            pos_feats = forward_samples(self.model, image, pos_examples)
            neg_feats = forward_samples(self.model, image, neg_examples)
            self.pos_feats_all.append(pos_feats)
            self.neg_feats_all.append(neg_feats)
            if len(self.pos_feats_all) > opts['n_frames_long']:
                del self.pos_feats_all[0]
            if len(self.neg_feats_all) > opts['n_frames_short']:
                del self.neg_feats_all[0]

        # Short term update
        if not success:
            print("not success")
            nframes = min(opts['n_frames_short'],len(self.pos_feats_all))
            pos_data = torch.stack(self.pos_feats_all[-nframes:],0).view(-1,self.feat_dim)
            neg_data = torch.stack(self.neg_feats_all,0).view(-1,self.feat_dim)
            train(self.model, self.criterion, self.update_optimizer, pos_data, neg_data, opts['maxiter_update'])

        # Long term update
        elif 1 % opts['long_interval'] == 0:
            pos_data = torch.stack(self.pos_feats_all,0).view(-1,self.feat_dim)
            neg_data = torch.stack(self.neg_feats_all,0).view(-1,self.feat_dim)
            train(self.model, self.criterion, self.update_optimizer, pos_data, neg_data, opts['maxiter_update'])

        #display on console

        #print(self.result_bb)



        dpi = 80.0

        drawn = image

        figsize = (8.0, 4.5)
        fig = plt.figure(frameon=False, figsize=figsize, dpi=80.0)
        ax = plt.Axes(fig, [0., 0., 1., 1.])
        ax.set_axis_off()
        fig.add_axes(ax)

        print(drawn)

        im = ax.imshow(drawn, aspect='auto')

        test = self.result_bb[self.frameNumber]

        rect = plt.Rectangle(tuple(test[0:2]),test[2],test[3],
                linewidth=3, edgecolor="#ff0000", zorder=1, fill=False)

        ax.add_patch(rect)


        print(drawn)
        plt.savefig("../result_fig/output/img" + str(self.frameNumber) + ".jpg")
        image=cv2.imread("../result_fig/output/img" + str(self.frameNumber) + ".jpg")
        cv2.imshow("image", image)
        cv2.waitKey(0)



    # Getters / Setters
    # TODO remove unnecessary getters/setters
    def getCoordinates():
        return self.coordinates


    def getCurrentCoordinates():
        return self.coordinates["current"]

    def getNextFrameNumber():
        return (self.frameNumber+1)
