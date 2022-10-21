import argparse
import os
import os.path
import pickle
import re
import time
from pathlib import Path

import cv2
import imgaug.augmenters as iaa
import numpy as np
import pandas as pd
from skimage.util import img_as_ubyte
from sklearn.metrics.pairwise import cosine_similarity
from tqdm import tqdm

from deeplabcut.pose_estimation_tensorflow.config import load_config
from deeplabcut.pose_estimation_tensorflow.core import \
    predict as single_predict
from deeplabcut.pose_estimation_tensorflow.core import \
    predict_multianimal as predict
from deeplabcut.pose_estimation_tensorflow.lib import inferenceutils
from deeplabcut.pose_estimation_tensorflow.lib import trackingutils
from deeplabcut.utils import auxfun_multianimal
from deeplabcut.utils import auxiliaryfunctions
from deeplabcut.utils.auxfun_videos import VideoWriter



def extract_bbox_from_file(filename):

    with open(filename, 'rb') as f:
        json_obj = json.load(f)
    bboxs = json_obj['bbox']

    return bboxs


def _topdown_reverse_transformation(preds, bbox):
    ret = {}

    num_kpts = len(preds[0]['coordinates'][0])
    ret['coordinates'] = [[[]]* num_kpts]
    ret['confidence'] = [[]] * num_kpts
                          
    for instance_id, pred in enumerate(preds):
        coordinate = pred['coordinates'][0]
        confidence = pred['confidence']
        
        for kpt_id, coord_list in enumerate(coordinate):

            if len(ret['coordinates'][0][kpt_id]) == 0:
                ret['coordinates'][0][kpt_id] = [[]] * len(preds)
                ret['confidence'][kpt_id] = [[]]* len(preds)
                
            if len(coord_list) == 0:                
                ret['coordinates'][0][kpt_id][instance_id] = np.array([np.nan, np.nan])
                ret['confidence'][kpt_id][instance_id] = np.array([np.nan])
                continue
            confidence_list = confidence[kpt_id]
            max_idx = np.argmax(confidence_list)
            # shifting the prediction
            x1,y1 = bbox[instance_id][:2]
            
            max_pred = coordinate[kpt_id][max_idx] + np.array([x1, y1])

            # only keep the max        
            confidence[kpt_id] = confidence_list[max_idx]
            coordinate[kpt_id] = max_pred

        
            ret['coordinates'][0][kpt_id][instance_id] = coordinate[kpt_id]
            ret['confidence'][kpt_id][instance_id] = confidence[kpt_id]
            
    return ret



def video_inference_topdown(
        cfg,
        test_cfg,
        sess,
        inputs,
        outputs,
        cap,
        nframes,
        batchsize,
        bboxs,
        invert_color = False,

):

    strwidth = int(np.ceil(np.log10(nframes)))  # width for strings
    batch_ind = 0  # keeps track of which image within a batch should be written to

    nx, ny = cap.dimensions

    pbar = tqdm(total=nframes)
    counter = 0
    inds = []
    PredicteData = {}
    # len(frames) -> (n_scale,)
    # frames[0].shape - > (batchsize, h, w, 3)
    
    while cap.video.isOpened():
        # no crop needed
        _frame = cap.read_frame()
        if _frame is not None:
            frame = img_as_ubyte(_frame)
            h,w,_ = frame.shape 

            bbox = bboxs[counter]
            preds = []
            _bbox = []
            black_mean = None
            for i in range(len(bbox)):

                x1, y1, x2, y2 = bbox[i]

                if x1<= 1.0 and x2<= 1.0 and y1<=1.0 and y2<=1.0:
                    # normalized bounding box
                    y1,y2 = int(h*y1), int(h*y2)
                    x1,x2 = int(w*x1), int(w*x2)
                    
                _bbox.append([x1,y1,x2,y2])
                    
                cropped_frame = frame[y1:y2, x1:x2]
                # hardcoded rule for cal database
                if i == 1:
                    cropped_frame = 255 - cropped_frame 
                cropped_frame = np.array(cropped_frame)
                cropped_frame = np.expand_dims(cropped_frame, axis = 0)
                    # batch full, start true inferencing
                D = predict.predict_batched_peaks_and_costs(
                    test_cfg, cropped_frame, sess, inputs, outputs
                )
                # stripping the batch dimension
                preds.append(D[0])

                    # only do this when animal is detected                                
            preds = _topdown_reverse_transformation(preds, _bbox)
                        
            PredicteData["frame" + str(counter).zfill(strwidth)] = preds 

        if counter>= nframes:
            break
        counter+=1
        pbar.update(1)
                                    
    cap.close()
    pbar.close()

            
    PredicteData["metadata"] = {
        "nms radius": test_cfg.get("nmsradius", None),
        "minimal confidence": test_cfg.get("minconfidence", None),
        "sigma": test_cfg.get("sigma", 1),
        "PAFgraph": test_cfg.get("partaffinityfield_graph",None),
        "PAFinds": test_cfg.get(
            "paf_best", np.arange(len(test_cfg["partaffinityfield_graph"]))
        ),
        "all_joints": [[i] for i in range(len(test_cfg["all_joints"]))],
        "all_joints_names": [
            test_cfg["all_joints_names"][i] for i in range(len(test_cfg["all_joints"]))
        ],
        "nframes": nframes,
    }
       
    return PredicteData, nframes
    


# instead of having these in a lengthy function, I made this a separate function
def get_nuances(
    config,
    videos,
    videotype="avi",
    shuffle=1,
    trainingsetindex=0,
    destfolder=None,
    batchsize=None,
    TFGPUinference=True,
    modelprefix="",
    robust_nframes=False,
    allow_growth=False,
    init_weights="",
    save_frames=False,
):

    cfg = auxiliaryfunctions.read_config(config)
    trainFraction = cfg["TrainingFraction"][trainingsetindex]
    modelfolder = os.path.join(
        cfg["project_path"],
        str(
            auxiliaryfunctions.get_model_folder(
                trainFraction, shuffle, cfg, modelprefix=modelprefix
            )
        ),
    )
    path_test_config = Path(modelfolder) / "test" / "pose_cfg.yaml"
    # called test_cfg instead of dlc_cfg to avoid confusion
    test_cfg = load_config(str(path_test_config))

    if init_weights:
        # this is for loading a stand alone supermodel checkpoint
        test_cfg["init_weights"] = init_weights

    # no more part affinity
    test_cfg["partaffinityfield_graph"] = []
    test_cfg["partaffinityfield_predict"] = False

    if init_weights == "":
        Snapshots = np.array(
            [
                fn.split(".")[0]
                for fn in os.listdir(os.path.join(modelfolder, "train"))
                if "index" in fn
            ]
        )
        snapshotindex = cfg["snapshotindex"]

        increasing_indices = np.argsort([int(m.split("-")[1]) for m in Snapshots])
        Snapshots = Snapshots[increasing_indices]

        test_cfg["init_weights"] = os.path.join(
            modelfolder, "train", Snapshots[snapshotindex]
        )
        trainingsiterations = (test_cfg["init_weights"].split(os.sep)[-1]).split("-")[
            -1
        ]

        DLCscorer, DLCscorerlegacy = auxiliaryfunctions.GetScorerName(
            cfg,
            shuffle,
            trainFraction,
            trainingsiterations=trainingsiterations,
            modelprefix=modelprefix,
        )
    else:
        Snapshots = [0]
        snapshotindex = 0
        DLCscorer = (
            f"{modelprefix}_{Path(init_weights).stem}"
            if modelprefix
            else Path(init_weights).stem
        )
        DLCscorerlegacy = DLCscorer

    print("Using %s" % Snapshots[snapshotindex], "for model", modelfolder)

    trainingsiterations = (test_cfg["init_weights"].split(os.sep)[-1]).split("-")[-1]
    # Update number of output and batchsize
    test_cfg["num_outputs"] = cfg.get("num_outputs", test_cfg.get("num_outputs", 1))

    if batchsize == None:
        # update batchsize (based on parameters in config.yaml)
        test_cfg["batch_size"] = cfg["batch_size"]
    else:
        test_cfg["batch_size"] = batchsize
        cfg["batch_size"] = batchsize

    if test_cfg["num_outputs"] > 1:
        if TFGPUinference:
            print(
                "Switching to numpy-based keypoint extraction code, as multiple point extraction is not supported by TF code currently."
            )
            TFGPUinference = False
        print("Extracting ", test_cfg["num_outputs"], "instances per bodypart")
        xyz_labs_orig = ["x", "y", "likelihood"]
        suffix = [str(s + 1) for s in range(test_cfg["num_outputs"])]
        suffix[0] = ""  # first one has empty suffix for backwards compatibility
        xyz_labs = [x + s for s in suffix for x in xyz_labs_orig]
    else:
        xyz_labs = ["x", "y", "likelihood"]

    sess, inputs, outputs = single_predict.setup_pose_prediction(
        test_cfg, allow_growth=allow_growth
    )

    pdindex = pd.MultiIndex.from_product(
        [[DLCscorer], test_cfg["all_joints_names"], xyz_labs],
        names=["scorer", "bodyparts", "coords"],
    )

    Videos = auxiliaryfunctions.get_list_of_videos(videos, videotype)

    ret = {}

    ret["cfg"] = cfg
    ret["videos"] = Videos
    ret["DLCscorer"] = DLCscorer
    ret["trainFraction"] = trainFraction
    ret["test_cfg"] = test_cfg
    ret["sess"] = sess
    ret["inputs"] = inputs
    ret["outputs"] = outputs
    ret["destfolder"] = destfolder
    ret["save_frames"] = save_frames
    ret["init_weights"] = init_weights

    return ret


def get_multi_scale_frames(frame, scale_list=[]):
    augs = []
    shapes = []
    for scale in scale_list:
        aug = iaa.Resize({"width": "keep-aspect-ratio", "height": scale})
        augs.append(aug)

    frames = []
    for i in range(len(scale_list)):
        resized_frame = augs[i](image=frame)
        frames.append(resized_frame)
        shapes.append(frames[-1].shape)

    return frames, shapes


def _project_pred_to_original_size(pred, old_shape, new_shape):

    old_h, old_w, _ = old_shape
    new_h, new_w, _ = new_shape
    ratio_h, ratio_w = old_h / new_h, old_w / new_w

    coordinate = pred["coordinates"][0]
    confidence = pred["confidence"]
    for kpt_id, coord_list in enumerate(coordinate):
        if len(coord_list) == 0:
            continue
        confidence_list = confidence[kpt_id]
        max_idx = np.argmax(confidence_list)
        # ratio_h and ratio_w should match though in reality it does not match exactly
        max_pred = coordinate[kpt_id][max_idx] * ratio_h

        # only keep the max

        confidence[kpt_id] = confidence_list[max_idx]
        coordinate[kpt_id] = max_pred
    return pred


def _average_multiple_scale_preds(preds, scale_list, cos_dist_threshold = 0.997, confidence_threshold = 0.1):

    ret_pred = {}
    num_kpts = len(preds[0]["coordinates"][0])
    ret_pred["coordinates"] = [[[]] * num_kpts]
    ret_pred["confidence"] = [[]] * num_kpts

    for scale_id, pred in enumerate(preds):
        # better handle the case where the pred is empty
        if not len(pred):
            coordinate = [[]] * num_kpts
            confidence = [[]] * num_kpts
        else:
            coordinate = pred["coordinates"][0]
            confidence = pred["confidence"]

        for kpt_id, coord_list in enumerate(coordinate):

            if len(ret_pred["coordinates"][0][kpt_id]) == 0:
                ret_pred["coordinates"][0][kpt_id] = [[]] * len(scale_list)
                ret_pred["confidence"][kpt_id] = [[]] * len(scale_list)

            temp_coord = np.expand_dims(coord_list, axis=0)
            ret_pred["coordinates"][0][kpt_id][scale_id] = temp_coord
            temp_confidence = np.expand_dims(confidence[kpt_id], axis=0)
            ret_pred["confidence"][kpt_id][scale_id] = temp_confidence

    for kpt_id in range(num_kpts):

        remove_indices = []
        for idx, ele in enumerate(ret_pred["coordinates"][0][kpt_id]):
            if len(ele[0]) == 0:
                remove_indices.append(idx)

        for idx, ele in enumerate(ret_pred["coordinates"][0][kpt_id]):
            if idx in remove_indices:
                # using [0,0] instead of [nan,nan] for cosine similarity to correctly pick up distances
                ret_pred["coordinates"][0][kpt_id][idx] = np.array([[0, 0]])
                ret_pred["confidence"][kpt_id][idx] = np.array([[0]])

        mean_vec = np.nanmedian(np.array(ret_pred["coordinates"][0][kpt_id]), axis=0)
        candidates = np.array(ret_pred["coordinates"][0][kpt_id])
        dist = []

        for i in range(len(candidates)):
            dist.append(cosine_similarity(candidates[i], mean_vec))

        filter_indices = []

        for idx, ele in enumerate(ret_pred["coordinates"][0][kpt_id]):
            if dist[idx] < cos_dist_threshold or ret_pred["confidence"][kpt_id][idx] < confidence_threshold:
                filter_indices.append(idx)

        for idx, ele in enumerate(ret_pred["coordinates"][0][kpt_id]):
            if idx in filter_indices:
                ret_pred["coordinates"][0][kpt_id][idx] = np.array([[np.nan, np.nan]])
                ret_pred["confidence"][kpt_id][idx] = np.array([[np.nan]])

        ret_pred["coordinates"][0][kpt_id] = np.concatenate(
            ret_pred["coordinates"][0][kpt_id], axis=0
        )
        ret_pred["confidence"][kpt_id] = np.concatenate(
            ret_pred["confidence"][kpt_id], axis=0
        )

        # need np.array for wrapping the list for evaluation code to work correctly
        ret_pred["coordinates"][0][kpt_id] = np.array([
            np.nanmedian(np.array(ret_pred["coordinates"][0][kpt_id]), axis=0)
        ])
        ret_pred["confidence"][kpt_id] = np.array(
            [np.nanmedian(np.array(ret_pred["confidence"][kpt_id]), axis=0)]
        )

    return ret_pred


def video_inference(
    cfg,
    test_cfg,
    sess,
    inputs,
    outputs,
    cap,
    nframes,
    batchsize,
    invert_color=False,
    scale_list=[],
):

    strwidth = int(np.ceil(np.log10(nframes)))  # width for strings
    batch_ind = 0  # keeps track of which image within a batch should be written to

    nx, ny = cap.dimensions

    pbar = tqdm(total=nframes)
    counter = 0
    inds = []
    print("scale list", scale_list)
    PredicteData = {}
    # len(frames) -> (n_scale,)
    # frames[0].shape - > (batchsize, h, w, 3)
    multi_scale_batched_frames = None
    frame_shapes = None
    while cap.video.isOpened():
        # no crop needed
        _frame = cap.read_frame()
        if _frame is not None:
            frame = img_as_ubyte(_frame)

            if invert_color:
                frame = 255 - frame

            old_shape = frame.shape
            frames, frame_shapes = get_multi_scale_frames(frame, scale_list)

            if multi_scale_batched_frames is None:
                multi_scale_batched_frames = [
                    np.empty(
                        (batchsize, frame.shape[0], frame.shape[1], 3), dtype="ubyte"
                    )
                    for frame in frames
                ]

            for scale_id, frame in enumerate(frames):
                multi_scale_batched_frames[scale_id][batch_ind] = frame
            inds.append(counter)
            if batch_ind == batchsize - 1:
                preds = []
                for scale_id, batched_frames in enumerate(multi_scale_batched_frames):
                    # batch full, start true inferencing
                    D = predict.predict_batched_peaks_and_costs(
                        test_cfg, batched_frames, sess, inputs, outputs
                    )
                    preds.append(D)

                    # only do this when animal is detected
                ind_start = inds[0]
                for i in range(batchsize):
                    ind = ind_start + i
                    PredicteData["frame" + str(ind).zfill(strwidth)] = []

                    for scale_id in range(len(scale_list)):
                        if i >= len(preds[scale_id]):
                            pred = []
                        else:
                            pred = preds[scale_id][i]
                        if pred != []:
                            pred = _project_pred_to_original_size(
                                pred, old_shape, frame_shapes[scale_id]
                            )

                        PredicteData["frame" + str(ind).zfill(strwidth)].append(pred)

                batch_ind = 0
                inds.clear()
            else:
                batch_ind += 1
        elif counter >= nframes:
            # in case we reach the end of the video
            if batch_ind > 0:
                preds = []
                for scale_id, batched_frames in enumerate(multi_scale_batched_frames):
                    D = predict.predict_batched_peaks_and_costs(
                        test_cfg,
                        batched_frames,
                        sess,
                        inputs,
                        outputs,
                    )

                    preds.append(D)

                ind_start = inds[0]
                for i in range(batchsize):
                    ind = ind_start + i
                    if ind >= nframes:
                        break
                    PredicteData["frame" + str(ind).zfill(strwidth)] = []
                    for scale_id in range(len(scale_list)):
                        if i >= len(preds[scale_id]):
                            pred = []
                        else:
                            pred = preds[scale_id][i]
                        if pred != []:
                            pred = _project_pred_to_original_size(
                                pred, old_shape, frame_shapes[scale_id]
                            )
                        PredicteData["frame" + str(ind).zfill(strwidth)].append(pred)

            break

        counter += 1
        pbar.update(1)

    cap.close()
    pbar.close()

    for k, v in PredicteData.items():
        if v != []:
            PredicteData[k] = _average_multiple_scale_preds(v, scale_list)

    PredicteData["metadata"] = {
        "nms radius": test_cfg.get("nmsradius", None),
        "minimal confidence": test_cfg.get("minconfidence", None),
        "sigma": test_cfg.get("sigma", 1),
        "PAFgraph": test_cfg.get("partaffinityfield_graph", None),
        "PAFinds": test_cfg.get(
            "paf_best", np.arange(len(test_cfg["partaffinityfield_graph"]))
        ),
        "all_joints": [[i] for i in range(len(test_cfg["all_joints"]))],
        "all_joints_names": [
            test_cfg["all_joints_names"][i] for i in range(len(test_cfg["all_joints"]))
        ],
        "nframes": nframes,
    }

    return PredicteData, nframes


def video_inference_supermodel(
    config,
    videos,
    scale_list=[],
    invert_color=False,
    videotype="avi",
    shuffle=1,
    trainingsetindex=0,
    destfolder=None,
    batchsize=None,
    TFGPUinference=True,
    modelprefix="",
    robust_nframes=False,
    allow_growth=False,
    init_weights="",
    save_frames=False,
    bbox_file = ''        
):
    """
    Makes prediction based on a super animal model. Note right now we only support single animal video inference

    The index of the trained network is specified by parameters in the config file (in particular the variable 'snapshotindex')

    Output: The labels are stored as MultiIndex Pandas Array, which contains the name of the network, body part name, (x, y) label position \n
            in pixels, and the likelihood for each frame per body part. These arrays are stored in an efficient Hierarchical Data Format (HDF) \n
            in the same directory, where the video is stored.

    Parameters
    ----------
    config: string
        Full path of the config.yaml file as a string.

    videos: list
        A list of strings containing the full paths to videos for analysis or a path to the directory, where all the videos with same extension are stored.

    scale_list: list
        A list of int containing the target height of the multi scale test time augmentation. By default it uses the original size. Users are advised to try a wide range of scale list when the super model does not give reasonable results


    videotype: string, optional
        Checks for the extension of the video in case the input to the video is a directory.\n Only videos with this extension are analyzed. The default is ``.avi``

    shuffle: int, optional
        An integer specifying the shuffle index of the training dataset used for training the network. The default is 1.

    trainingsetindex: int, optional
        Integer specifying which TrainingsetFraction to use. By default the first (note that TrainingFraction is a list in config.yaml).

    destfolder: string, optional
        Specifies the destination folder for analysis data (default is the path of the video). Note that for subsequent analysis this
        folder also needs to be passed.

    batchsize: int, default from pose_cfg.yaml
        Change batch size for inference; if given overwrites value in pose_cfg.yaml

    TFGPUinference: bool, default: True
        Perform inference on GPU with TensorFlow code. Introduced in "Pretraining boosts out-of-domain robustness for pose estimation" by
        Alexander Mathis, Mert Yüksekgönül, Byron Rogers, Matthias Bethge, Mackenzie W. Mathis Source: https://arxiv.org/abs/1909.11229

    robust_nframes: bool, optional (default=False)
        Evaluate a video's number of frames in a robust manner.
        This option is slower (as the whole video is read frame-by-frame),
        but does not rely on metadata, hence its robustness against file corruption.

    allow_growth: bool, default false.
        For some smaller GPUs the memory issues happen. If true, the memory allocator does not pre-allocate the entire specified
        GPU memory region, instead starting small and growing as needed. See issue: https://forum.image.sc/t/how-to-stop-running-out-of-vram/30551/2

    save_frames: bool, default false.
        If video adaptation is needed later, this flag should be set as True

    init_weights: string, default empty
        The path to the tensorflow checkpoint of the super model. Make sure you don't include the suffix of the snapshot file.

    bbox_file: string, default empty
        The path to a json file that contains bounding box

    Examples:

    Given a list of scales for spatial pyramid, i.e. [600, 700]

    scale_list = range(600,800,100)

    init_weights = PATH_TO_SUPERMODEL

    deeplabcut.video_inference_supermodel(config_path,
                                      [video_path],
                                      videotype=videotype,
                                      init_weights = init_weights,
                                      scale_list = scale_list,
                                      invert_color = False,   
                                      bbox_file = bbox_file)
    

    deeplabcut.create_labeled_video(config_path,
                                [video_path],
                                videotype = videotype,
                                init_weights = init_weights,
                                )


    """

    setting = get_nuances(
        config,
        videos,
        videotype=videotype,
        shuffle=shuffle,
        trainingsetindex=trainingsetindex,
        destfolder=destfolder,
        batchsize=batchsize,
        TFGPUinference=TFGPUinference,
        modelprefix=modelprefix,
        robust_nframes=robust_nframes,
        allow_growth=allow_growth,
        init_weights=init_weights,
        save_frames=save_frames,
    )

    test_cfg = setting["test_cfg"]
    cfg = setting["cfg"]
    videos = setting["videos"]
    destfolder = setting["destfolder"]
    DLCscorer = setting["DLCscorer"]
    sess = setting["sess"]
    inputs = setting["inputs"]
    outputs = setting["outputs"]
    trainFraction = setting["trainFraction"]

    for video in videos:
        vname = Path(video).stem

        if len(scale_list) == 0:

            # if the scale_list is empty, by default we use the original one
            vid = cv2.VideoCapture(video)
            h, w = int(vid.get(cv2.CAP_PROP_FRAME_HEIGHT)), int(
                vid.get(cv2.CAP_PROP_FRAME_WIDTH)
            )
            scale_list = [h]

        if bbox_file!='':
            bboxs = extract_bbox_from_file(bbox_file)        
            
        
        videofolder = str(Path(video).parents[0])
        if destfolder is None:
            destfolder = videofolder
            auxiliaryfunctions.attempttomakefolder(destfolder)

        dataname = os.path.join(destfolder, vname + DLCscorer + ".h5")

        if os.path.isfile(dataname):
            print("Video already analyzed!", dataname)
        else:
            print("Loading ", video)
            vid = VideoWriter(video)
            # need a separate writer for writing frames
            # otherwise it interrupts the prediction
            writer = VideoWriter(video)
            if robust_nframes:
                nframes = vid.get_n_frames(robust=True)
                duration = vid.calc_duration(robust=True)
                fps = nframes / duration
            else:
                nframes = len(vid)
                duration = vid.calc_duration(robust=False)
                fps = vid.fps

            if save_frames:
                if not os.path.exists(os.path.join(videofolder, vname)):
                    auxiliaryfunctions.attempttomakefolder(
                        os.path.join(videofolder, vname)
                    )

                    for n in range(nframes):
                        print(f"Writing frame {n + 1}|{nframes}")
                        frame = writer.read_frame()
                        writer.write_frame(
                            frame, os.path.join(videofolder, vname, f"frame_{n}.png")
                        )
                    writer.close()

            nx, ny = vid.dimensions
            print(
                "Duration of video [s]: ",
                round(duration, 2),
                ", recorded with ",
                round(fps, 2),
                "fps!",
            )
            print(
                "Overall # of frames: ",
                nframes,
                " found with (before cropping) frame dimensions: ",
                nx,
                ny,
            )
            start = time.time()

            print("Starting to extract posture")

            # extra data
            print("before inference")
            if bbox_file == '':
                PredicteData, nframes = video_inference(
                    cfg,
                    test_cfg,
                    sess,
                    inputs,
                    outputs,
                    vid,
                    nframes,
                    int(test_cfg["batch_size"]),
                    invert_color=invert_color,
                    scale_list=scale_list,
                )

            else:
                PredicteData, nframes = video_inference_topdown(
                    cfg,
                    test_cfg,
                    sess,
                    inputs,
                    outputs,
                    vid,
                    nframes,
                    int(test_cfg["batch_size"]),
                    bboxs,                    
                    invert_color = invert_color,

                )
                

            stop = time.time()

            coords = [0, nx, 0, ny]

            if cfg["cropping"] == True:
                coords = [cfg["x1"], cfg["x2"], cfg["y1"], cfg["y2"]]
            else:
                coords = [0, nx, 0, ny]

            dictionary = {
                "start": start,
                "stop": stop,
                "run_duration": stop - start,
                "Scorer": DLCscorer,
                "DLC-model-config file": test_cfg,
                "fps": fps,
                "batch_size": test_cfg["batch_size"],
                "frame_dimensions": (ny, nx),
                "nframes": nframes,
                "iteration (active-learning)": cfg["iteration"],
                "cropping": cfg["cropping"],
                "training set fraction": trainFraction,
                "cropping_parameters": coords,
            }
            metadata = {"data": dictionary}
            print("Saving results in %s..." % (destfolder))

            metadata_path = dataname.split(".h5")[0] + "_meta.pickle"

            with open(metadata_path, "wb") as f:
                pickle.dump(metadata, f, pickle.HIGHEST_PROTOCOL)

            xyz_labs = ["x", "y", "likelihood"]

            scorer = DLCscorer


            keypoint_names = test_cfg["all_joints_names"]

            if bbox_file !='':
                num_individuals = len(bboxs[0])
                individuals = [f'individual{id}' for id in range(num_individuals)]
            
            product = [ [scorer], keypoint_names, xyz_labs] if bbox_file == "" else [ [scorer], individuals, keypoint_names, xyz_labs]
            names = ['scorer', 'bodyparts', 'coords'] if bbox_file == "" else ['scorer', 'individuals', 'bodyparts', 'coords']
            
            columnindex = pd.MultiIndex.from_product(product,names = names)                                                                  
            imagenames = [k for k in  PredicteData.keys() if k !='metadata']

            data = np.zeros((len(imagenames), len(columnindex))) * np.nan
                                    
            df = pd.DataFrame(data, columns=columnindex, index=imagenames)


            for imagename in imagenames:

                if PredicteData[imagename] == []:

                    for kpt_id, kpt_name in enumerate(keypoint_names):
                        df.loc[imagename][scorer, kpt_name, "x"] = np.nan
                        df.loc[imagename][scorer, kpt_name, "y"] = np.nan
                        df.loc[imagename][scorer, kpt_name, "likelihood"] = 0
                    continue
                keypoints = PredicteData[imagename]["coordinates"][0]
                for kpt_id, kpt_name in enumerate(keypoint_names):

                    confidence = PredicteData[imagename]["confidence"]

                    if bbox_file == '':
                    
                        df.loc[imagename][scorer,  kpt_name, 'x'] = keypoints[kpt_id][0][0]
                        df.loc[imagename][scorer,  kpt_name, 'y'] = keypoints[kpt_id][0][1]
                        df.loc[imagename][scorer,  kpt_name, 'likelihood'] = confidence[kpt_id]

                    else:
                        for individual_id in range(len(individuals)):
                            
                            
                            df.loc[imagename][scorer, f'individual{individual_id}',  kpt_name, 'x'] = keypoints[kpt_id][individual_id][0]
                            df.loc[imagename][scorer, f'individual{individual_id}', kpt_name, 'y' ] = keypoints[kpt_id][individual_id][1]
                            df.loc[imagename][scorer, f'individual{individual_id}',  kpt_name, 'likelihood'] = confidence[kpt_id][individual_id]
                    

            df.to_hdf(dataname, "df_with_missing", format="table", mode="w")
