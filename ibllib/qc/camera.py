"""Camera QC
This module runs a list of quality control metrics on the camera and extracted video data.

Example - Run camera QC, downloading all but video file
    qc = CameraQC(eid, download_data=True, stream=True)
    qc.run()

TODO Remove notes
Question:
    We're not extracting the audio based on TTL length.  Is this a problem?
    For hist equalization:
        cvAddWeighted( )" ?

        What it does is:

                     dst = src1*alpha + src2*beta + gamma

        Applying brightness and contrast:

                     dst = src*contrast + brightness;

        so if

                     src1  = input image
                     src2  = any image of same type as src1
                     alpha = contrast value
                     beta  = 0.0
                     gamma = brightness value
                     dst   = resulting Image (must be of same type as src1)

"""
import logging
from inspect import getmembers, isfunction
from pathlib import Path

import cv2
import numpy as np
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle

from ibllib.io.extractors.camera import (
    extract_camera_sync, PIN_STATE_THRESHOLD, extract_all
)
from ibllib.exceptions import ALFObjectNotFound
from ibllib.io.extractors import ephys_fpga, training_wheel
from ibllib.io.extractors.base import get_session_extractor_type
from ibllib.io import raw_data_loaders as raw
import alf.io as alfio
from brainbox.core import Bunch
from brainbox.video.motion import MotionAlignment
from ibllib.io.video import get_video_meta, get_video_frames_preload
from . import base

_log = logging.getLogger('ibllib')


class CameraQC(base.QC):
    """A class for computing camera QC metrics"""
    dstypes = [
        '_iblrig_Camera.frame_counter',
        '_iblrig_Camera.GPIO',
        '_iblrig_Camera.timestamps',
        '_iblrig_taskData.raw',
        '_iblrig_taskSettings.raw',
        '_iblrig_Camera.raw',
        'camera.times',
        'wheel.position',
        'wheel.timestamps',
    ]
    dstypes_fpga = [
        '_spikeglx_sync.channels',
        '_spikeglx_sync.polarities',
        '_spikeglx_sync.times',
        'ephysData.raw.meta',
        'ephysData.raw.wiring'
    ]
    """Recall that for the training rig there is only one side camera at 30 Hz and 1280 x 1024 px. 
    For the recording rig there are two side cameras (left: 60 Hz, 1280 x 1024 px; 
    right: 150 Hz, 640 x 512 px) and one body camera (30 Hz, 640 x 512 px). """
    video_meta = {
        'training': {
            'left': {
                'fps': 30,
                'width': 1280,
                'height': 1024
            }
        },
        'ephys': {
            'left': {
                'fps': 60,
                'width': 1280,
                'height': 1024
            },
            'right': {
                'fps': 150,
                'width': 640,
                'height': 512
            },
            'body': {
                'fps': 30,
                'width': 640,
                'height': 512
            },
        }
    }

    def __init__(self, session_path_or_eid, side, **kwargs):
        """
        :param session_path_or_eid: A session eid or path
        :param log: A logging.Logger instance, if None the 'ibllib' logger is used
        :param one: An ONE instance for fetching and setting the QC on Alyx
        :param camera: The camera to run QC on, if None QC is run for all three cameras
        :param stream: If true and local video files not available, the data are streamed from
        the remote source.
        :param n_samples: The number of frames to sample for the position and brightness QC
        """
        # When an eid is provided, we will download the required data by default (if necessary)
        download_data = not alfio.is_session_path(session_path_or_eid)
        self.download_data = kwargs.pop('download_data', download_data)
        self.stream = kwargs.pop('stream', True)
        self.n_samples = kwargs.pop('n_samples', 100)
        super().__init__(session_path_or_eid, **kwargs)

        # Data
        self.side = side
        filename = f'_iblrig_{self.side}Camera.raw.mp4'
        self.video_path = self.session_path / 'raw_video_data' / filename
        # If local video doesn't exist, change video path to URL
        if not self.video_path.exists() and self.stream and self.one is not None:
            self.video_path = self.one.url_from_path(self.video_path)

        logging.disable(logging.CRITICAL)
        self.type = get_session_extractor_type(self.session_path) or None
        logging.disable(logging.NOTSET)
        keys = ('count', 'pin_state', 'audio', 'fpga_times', 'wheel', 'video', 'frame_samples')
        self.data = Bunch.fromkeys(keys)
        self.frame_samples_idx = None

        # QC outcomes map
        self.metrics = None
        self.outcome = 'NOT_SET'

    def load_data(self, download_data: bool = None, extract_times: bool = False) -> None:
        """Extract the data from raw data files
        Extracts all the required task data from the raw data files.

        :param download_data: if True, any missing raw data is downloaded via ONE.
        :param extract_times: if True, the camera.times are re-extracted from the raw data
        """
        if download_data is not None:
            self.download_data = download_data
        if self.one:
            self._ensure_required_data()
        _log.info('Gathering data for QC')

        # Get frame count and pin state
        self.data['count'], self.data['pin_state'] = \
            raw.load_embedded_frame_data(self.session_path, self.side, raw=True)

        # Get audio and wheel data
        wheel_keys = ('timestamps', 'position')
        alf_path = self.session_path / 'alf'
        try:
            self.data['wheel'] = alfio.load_object(alf_path, 'wheel')
        except ALFObjectNotFound:
            # Extract from raw data
            if self.type == 'ephys':
                sync, chmap = ephys_fpga.get_main_probe_sync(self.session_path)
                wheel_data = ephys_fpga.extract_wheel_sync(sync, chmap)
                audio_ttls = ephys_fpga._get_sync_fronts(sync, chmap['audio'])
                self.data['audio'] = audio_ttls['times'][::2]  # Get rises
                # Load raw FPGA times
                cam_ts = extract_camera_sync(sync, chmap)
                self.data['fpga_times'] = cam_ts[self.side]
            else:
                bpod_data = raw.load_data(self.session_path)
                wheel_data = training_wheel.get_wheel_position(self.session_path)
                _, audio_ttls = raw.load_bpod_fronts(self.session_path, bpod_data)
                self.data['audio'] = audio_ttls['times'][::2]
            self.data['wheel'] = Bunch(zip(wheel_keys, wheel_data))

        # Find short period of wheel motion for motion correlation.  For speed start with the
        # fist 2 minutes (nearly always enough), extract wheel movements and pick one.
        # TODO Pick movement towards the end of the session (but not right at the end as some
        #  are extrapolated).  Make sure the movement isn't too long.
        if not any(x is None for x in self.data['wheel']):
            START = 1 * 60  # Start 1 minute in
            SEARCH_PERIOD = 2 * 60
            ts, pos = [self.data['wheel'][k] for k in wheel_keys]
            while True:
                win = np.logical_and(
                    ts > START,
                    ts < SEARCH_PERIOD + START
                )
                if np.sum(win) > 1000:
                    break
                SEARCH_PERIOD *= 2
            wheel_moves = training_wheel.extract_wheel_moves(ts[win], pos[win])
            move_ind = np.argmax(np.abs(wheel_moves['peakAmplitude']))
            # TODO Save only the wheel fragment we need
            self.data['wheel'].period = wheel_moves['intervals'][move_ind, :]

        # Load extracted frame times
        try:
            assert not extract_times
            self.data['fpga_times'] = alfio.load_object(alf_path, f'{self.side}Camera')['times']
        except AssertionError:  # Re-extract
            kwargs = dict(video_path=self.video_path, labels=self.side)
            if self.type == 'ephys':
                sync, chmap = ephys_fpga.get_main_probe_sync(self.session_path)
                kwargs = {**kwargs, 'sync': sync, 'chmap': chmap}  # noqa
            outputs, _ = extract_all(self.session_path, self.type, save=False, **kwargs)
            self.data['fpga_times'] = outputs[f'{self.side}_camera_timestamps']
        except ALFObjectNotFound:
            _log.warning('no camera.times ALF found for session')

        # Gather information from video file
        _log.info('Inspecting video file...')
        self.load_video_data()

        # Load Bonsai frame timestamps
        try:
            ssv_times = raw.load_camera_ssv_times(self.session_path, self.side)
            self.data['bonsai_times'], self.data['camera_times'] = ssv_times
        except AssertionError:
            _log.warning('No Bonsai video timestamps file found')

    def load_video_data(self):
        # Get basic properties of video
        try:
            self.data['video'] = get_video_meta(self.video_path, one=self.one)
            # Sample some frames from the video file
            indices = np.linspace(100, self.data['video'].length - 100, self.n_samples).astype(int)
            self.frame_samples_idx = indices
            self.data['frame_samples'] = get_video_frames_preload(self.video_path, indices,
                                                                  mask=np.s_[:, :, 0])
        except AssertionError:
            _log.error('Failed to read video file; setting outcome to CRITICAL')
            self._outcome = 'CRITICAL'

    def _ensure_required_data(self):
        """
        Ensures the datasets required for QC are local.  If the download_data attribute is True,
        any missing data are downloaded.  If all the data are not present locally at the end of
        it an exception is raised.  If the stream attribute is True, the video file is not
        required to be local, however it must be remotely accessible.

        TODO make static method with side as optional arg
        :return:
        """
        assert self.one is not None, 'ONE required to download data'
        # Get extractor type
        is_ephys = 'ephys' in (self.type or self.one.get_details(self.eid)['task_protocol'])
        dtypes = self.dstypes + self.dstypes_fpga if is_ephys else self.dstypes
        for dstype in dtypes:
            dataset = self.one.datasets_from_type(self.eid, dstype)
            if 'camera' in dstype.lower():  # Download individual camera file
                dataset = [d for d in dataset if self.side in d]
            if any(x.endswith('.mp4') for x in dataset) and self.stream:
                names = [x.name for x in self.one.list(self.eid)]
                assert f'_iblrig_{self.side}Camera.raw.mp4' in names, 'No remote video file found'
                continue
            optional = ('camera.times', '_iblrig_Camera.raw', 'wheel.position',
                        'wheel.timestamps', '_iblrig_Camera.frame_counter', '_iblrig_Camera.GPIO')
            required = (dstype not in optional)
            collection = 'raw_behavior_data' if dstype == '_iblrig_taskSettings.raw' else None
            kwargs = {'download_only': True, 'collection': collection}
            present = (
                (self.one.load_dataset(self.eid, d, **kwargs) for d in dataset)
                if self.download_data
                else (next(self.session_path.rglob(d), None) for d in dataset)
            )
            assert (dataset and all(present)) or not required, f'Dataset {dstype} not found'
        self.type = get_session_extractor_type(self.session_path)

    def run(self, update: bool = False, **kwargs) -> (str, dict):
        """
        Run video QC checks and return outcome
        :param update: if True, updates the session QC fields on Alyx
        :param download_data: if True, downloads any missing data if required
        :param extract_times: if True, re-extracts the camera timestamps from the raw data
        :returns: overall outcome as a str, a dict of checks and their outcomes
        TODO Ensure that when pin state QC NOT_SET it is not used in overall outcome
        """
        # TODO Use exp ref here
        _log.info(f'Computing QC outcome for {self.side} camera, session {self.eid}')
        namespace = f'video{self.side.capitalize()}'
        if all(x is None for x in self.data.values()):
            self.load_data(**kwargs)
        if self.data['frame_samples'] is None:
            return 'NOT_SET', {}

        def is_metric(x):
            return isfunction(x) and x.__name__.startswith('check_')

        checks = getmembers(CameraQC, is_metric)
        self.metrics = {f'_{namespace}_' + k[6:]: fn(self) for k, fn in checks}

        # all_pass = all(x is None or x == 'PASS' for x in self.metrics.values())
        # outcome = 'PASS' if all_pass else 'FAIL'
        code = max(base.CRITERIA[x] for x in self.metrics.values())
        outcome = next(k for k, v in base.CRITERIA.items() if v == code)

        if update:
            bool_map = {k: None if v is None else v == 'PASS' for k, v in self.metrics.items()}
            self.update_extended_qc(bool_map)
            self.update(outcome, namespace)
        return outcome, self.metrics

    def check_brightness(self, bounds=(40, 200), max_std=20, display=False):
        """Check that the video brightness is within a given range
        The mean brightness of each frame must be with the bounds provided, and the standard
        deviation across samples frames should be less then the given value.  Assumes that the
        frame samples are 2D (no colour channels).

        :param bounds: For each frame, check that: bounds[0] < M < bounds[1], where M = mean(frame)
        :param max_std: The standard deviation of the frame luminance means must be less than this
        :param display: When True the mean frame luminance is plotted against sample frames.
        The sample frames with the lowest and highest mean luminance are shown.
        """
        if self.data['frame_samples'] is None:
            return 'NOT_SET'
        brightness = self.data['frame_samples'].mean(axis=(1, 2))
        # dims = self.data['frame_samples'].shape
        # brightness /= np.array((*dims[1:], 255)).prod()  # Normalize

        within_range = np.logical_and(brightness > bounds[0],
                                      brightness < bounds[1])
        passed = within_range.all() and np.std(brightness) < max_std
        if display:
            f = plt.figure()
            gs = f.add_gridspec(2, 3)
            indices = self.frame_samples_idx
            # Plot mean frame luminance
            ax = f.add_subplot(gs[:2, :2])
            plt.plot(indices, brightness, label='brightness')
            ax.set(
                xlabel='frame #',
                ylabel='brightness (mean pixel)',
                title='Brightness')
            ax.hlines(bounds, 0, indices[-1], colors='r', linestyles=':', label='bounds')
            ax.legend()
            # Plot min-max frames
            for i, idx in enumerate((np.argmax(brightness), np.argmin(brightness))):
                a = f.add_subplot(gs[i, 2])
                ax.annotate('*', (indices[idx], brightness[idx]),  # this is the point to label
                            textcoords="offset points", xytext=(0, 1),  ha='center')
                frame = self.data['frame_samples'][idx]
                title = ('min' if i else 'max') + ' mean luminance = %.2f' % brightness[idx]
                self.imshow(frame, ax=a, title=title)
        return 'PASS' if passed else 'FAIL'

    def check_file_headers(self):
        """Check reported frame rate matches FPGA frame rate"""
        if None in (self.data['video'], self.video_meta):
            return 'NOT_SET'
        expected = self.video_meta[self.type][self.side]
        return 'PASS' if self.data['video']['fps'] == expected['fps'] else 'FAIL'

    def check_framerate(self, threshold=1.):
        """Check camera times match specified frame rate for camera

        :param threshold: The maximum absolute difference between timestamp sample rate and video
        frame rate.  NB: Does not take into account dropped frames.
        """
        if any(x is None for x in (self.data['fpga_times'], self.video_meta)):
            return 'NOT_SET'
        fps = self.video_meta[self.type][self.side]['fps']
        Fs = 1 / np.median(np.diff(self.data['fpga_times']))  # Approx. frequency of camera
        return 'PASS' if abs(Fs - fps) < threshold else 'FAIL'

    def check_pin_state(self, display=False):
        """Check the pin state reflects Bpod TTLs
        TODO Return WARNING if more GPIOs elements than timestamps, FAIL if fewer GPIO elements
         than frame times, or check audio events don't happen at end
        """
        if self.data['pin_state'] is None:
            return 'NOT_SET'
        size_matches = self.data['video']['length'] == self.data['pin_state'].size
        # There should be only one value below our threshold
        binary = np.unique(self.data['pin_state']).size == 2
        state = self.data['pin_state'] > PIN_STATE_THRESHOLD
        # NB: The pin state to be high for 2 consecutive frames
        low2high = np.insert(np.diff(state.astype(int)) == 1, 0, False)
        # NB: Time between two consecutive TTLs can be sub-frame, so this will fail
        state_ttl_matches = sum(low2high) == self.data['audio'].size
        # Check within ms of audio times
        if display:
            plt.Figure()
            plt.plot(self.data['fpga_times'][low2high], np.zeros(sum(low2high)), 'o',
                     label='GPIO Low -> High')
            plt.plot(self.data['audio'], np.zeros(self.data['audio'].size), 'rx',
                     label='Audio TTL High')
            plt.xlabel('FPGA frame times / s')
            plt.gca().set(yticklabels=[])
            plt.gca().tick_params(left=False)
            plt.legend()
        # idx = [i for i, x in enumerate(self.data['audio'])
        #        if np.abs(x - self.data['fpga_times'][low2high]).min() > 0.01]
        # mins = [np.abs(x - self.data['fpga_times'][low2high]).min() for x in self.data['audio']]

        return 'PASS' if size_matches and binary and state_ttl_matches else 'FAIL'

    def check_dropped_frames(self, threshold=.1):
        """Check how many frames were reported missing

        :param threshold: The maximum allowable percentage of dropped frames
        """
        if self.data['video'] is None or self.data['count'] is None:
            return 'NOT_SET'
        size_matches = self.data['video']['length'] == self.data['count'].size
        strict_increase = np.diff(self.data['count']) > 0
        if not np.all(strict_increase):
            n_effected = np.sum(np.invert(strict_increase))
            _log.info(f'frame count not strictly increasing: '
                      f'{n_effected} frames effected ({n_effected / strict_increase.size:.2%})')
            return 'CRITICAL'
        dropped = np.diff(self.data['count']).astype(int) - 1
        pct_dropped = (sum(dropped) / len(dropped) * 100)
        return 'PASS' if size_matches and pct_dropped < threshold else 'FAIL'

    def check_timestamps(self):
        """Check that the camera.times array is reasonable"""
        if self.data['fpga_times'] is None or self.data['video'] is None:
            return 'NOT_SET'
        # Check frame rate matches what we expect
        expected = 1 / self.video_meta[self.type][self.side]['fps']
        # TODO Remove dropped frames from test
        frame_delta = np.diff(self.data['fpga_times'])
        fps_matches = np.isclose(frame_delta.mean(), expected, atol=0.001)
        # Check number of timestamps matches video
        length_matches = self.data['fpga_times'].size == self.data['video'].length
        # Check times are strictly increasing
        increasing = all(np.diff(self.data['fpga_times']) > 0)
        # Check times do not contain nans
        nanless = not np.isnan(self.data['fpga_times']).any()
        return 'PASS' if increasing and fps_matches and length_matches and nanless else 'FAIL'

    def check_resolution(self):
        """Check that the timestamps and video file resolution match what we expect"""
        if self.data['video'] is None:
            return 'NOT_SET'
        actual = self.data['video']
        expected = self.video_meta[self.type][self.side]
        match = actual['width'] == expected['width'] and actual['height'] == expected['height']
        return 'PASS' if match else 'FAIL'

    def check_wheel_alignment(self, tolerance=1, display=False):
        """Check wheel motion in video correlates with the rotary encoder signal"""
        if self.data['wheel'] is None or self.side == 'body':
            return 'NOT_SET'

        aln = MotionAlignment(self.eid, self.one, self.log)
        aln.data = self.data
        aln.data['camera_times'] = {self.side: self.data['fpga_times']}
        aln.video_paths = {self.side: self.video_path}
        offset, *_ = aln.align_motion(period=self.data['wheel'].period,
                                      display=display, side=self.side)
        if display:
            aln.plot_alignment()
        return 'PASS' if np.abs(offset) <= tolerance else 'FAIL'

    def check_position(self, hist_thresh=(75, 80), pos_thresh=(10, 15),
                       metric=cv2.TM_CCOEFF_NORMED,
                       display=False, test=False, roi=None, pct_thresh=True):
        """Check camera is positioned correctly
        For the template matching zero-normalized cross-correlation (default) should be more
        robust to exposure (which we're not checking here).  The L2 norm (TM_SQDIFF) should
        also work.

        If display is True, the template ROI (pick hashed) is plotted over a video frame,
        along with the threshold regions (green solid).  The histogram correlations are plotted
        and the full histogram is plotted for one of the sample frames and the reference frame.

        :param hist_thresh: The minimum histogram cross-correlation threshold to pass (0-1).
        :param pos_thresh: The maximum number of pixels off that the template matcher may be off
         by. If two values are provided, the lower threshold is treated as a warning boundary.
        :param metric: The metric to use for template matching.
        :param display: If true, the results are plotted
        :param test: If true a reference frame instead of the frames in frame_samples.
        :param roi: A tuple of indices for the face template in the for ((y1, y2), (x1, x2))
        :param pct_thresh: If true, the thresholds are treated as percentages
        """
        if not test and self.data['frame_samples'] is None:
            return 'NOT_SET'
        refs = self.load_reference_frames(self.side)
        # ensure iterable
        pos_thresh = np.sort(np.array(pos_thresh))
        hist_thresh = np.sort(np.array(hist_thresh))

        # Method 1: compareHist
        ref_h = cv2.calcHist([refs[0]], [0], None, [256], [0, 256])
        frames = refs if test else self.data['frame_samples']
        hists = [cv2.calcHist([x], [0], None, [256], [0, 256]) for x in frames]
        corr = np.array([cv2.compareHist(test_h, ref_h, cv2.HISTCMP_CORREL) for test_h in hists])
        if pct_thresh:
            corr *= 100
        hist_passed = [np.all(corr > x) for x in hist_thresh]

        # Method 2:
        top_left, roi, template = self.find_face(roi=roi, test=test, metric=metric, refs=refs)
        (y1, y2), (x1, x2) = roi
        err = (x1, y1) - np.median(np.array(top_left), axis=0)
        h, w = frames[0].shape[:2]

        if pct_thresh:  # Threshold as percent
            # t_x, t_y = pct_thresh
            err_pct = [(abs(x) / y) * 100 for x, y in zip(err, (h, w))]
            face_passed = [all(err_pct < x) for x in pos_thresh]
        else:
            face_passed = [np.all(np.abs(err) < x) for x in pos_thresh]

        if display:
            plt.figure()
            # Plot frame with template overlay
            img = frames[0]
            ax0 = plt.subplot(221)
            ax0.imshow(img, cmap='gray', vmin=0, vmax=255)
            bounds = (x1 - err[0], x2 - err[0], y2 - err[1], y1 - err[1])
            ax0.imshow(template, cmap='gray', alpha=0.5, extent=bounds)
            if pct_thresh:
                for c, thresh in zip(('green', 'yellow'), pos_thresh):
                    t_y = (h / 100) * thresh
                    t_x = (w / 100) * thresh
                    xy = (x1 - t_x, y1 - t_y)
                    ax0.add_patch(Rectangle(xy, x2 - x1 + (t_x * 2), y2 - y1 +(t_y * 2),
                                            fill=True, facecolor=c, lw=0, alpha=0.05))
            else:
                for c, thresh in zip(('green', 'yellow'), pos_thresh):
                    xy = (x1 - thresh, y1 - thresh)
                    ax0.add_patch(Rectangle(xy, x2 - x1 + (thresh * 2), y2 - y1 + (thresh * 2),
                                            fill=True, facecolor=c, lw=0, alpha=0.05))
            xy = (x1 - err[0], y1 - err[1])
            ax0.add_patch(Rectangle(xy, x2-x1, y2-y1,
                                    edgecolor='pink', fill=False, hatch='//', lw=1))
            ax0.set(xlim=(0, img.shape[1]), ylim=(img.shape[0], 0))
            ax0.set_axis_off()
            # Plot the image histograms
            ax1 = plt.subplot(212)
            ax1.plot(ref_h[5:-1], label='reference frame')
            ax1.plot(np.array(hists).mean(axis=0)[5:-1], label='mean frame')
            ax1.set_xlim([0, 256])
            plt.legend()
            # Plot the correlations for each sample frame
            ax2 = plt.subplot(222)
            ax2.plot(corr, label='hist correlation')
            ax2.axhline(hist_thresh[0], 0, self.n_samples,
                        linestyle=':', color='r', label='fail threshold')
            ax2.axhline(hist_thresh[1], 0, self.n_samples,
                        linestyle=':', color='g', label='pass threshold')
            ax2.set(xlabel='Sample Frame #', ylabel='Hist correlation')
            plt.legend()
            plt.suptitle('Check position')
            plt.show()

        pass_map = {i: s for i, s in enumerate(('FAIL', 'WARNING', 'PASS'))}
        face_aligned = pass_map[sum(face_passed)]
        hist_correlates = pass_map[sum(hist_passed)]

        return self.overall_outcome([face_aligned, hist_correlates])

    def check_focus(self, n=20, threshold=(100, 6),
                    roi=False, display=False, test=False, equalize=True):
        """Check video is in focus
        Two methods are used here: Looking at the high frequencies with a DFT and
        applying a Laplacian HPF and looking at the variance.

        Note:
            - Both methods are sensitive to noise (Laplacian is 2nd order filter).
            - The thresholds for the fft may need to be different for the left/right vs body as
              the distribution of frequencies in the image is different (e.g. the holder
              comprises mostly very high frequencies).
            - The image may be overall in focus but the places we care about can still be out of
              focus (namely the face).  For this we'll take an ROI around the face.
            - Focus check thrown off by brightness.  This may be fixed by equalizing the histogram
              (set equalize=True)

        :param n: number of frames from frame_samples data to use in check.
        :param threshold: the lower boundary for Laplacian variance and mean FFT filtered
         brightness, respectively
        :param roi: if False, the roi is determined via template matching for the face or body.
        If None, some set ROIs for face and paws are used.  A list of slices may also be passed.
        :param display: if true, the results are displayed
        :param test: if true, a set of artificially blurred reference frames are used as the
        input.  This can be used to selecting reasonable thresholds.
        :param equalize: if true, the histograms of the frames are equalized, resulting in an
        increased the global contrast and linear CDF.  This makes check robust to low light
        conditions.
        """
        if not test and self.data['frame_samples'] is None:
            return 'NOT_SET'

        if roi == False:
            top_left, roi, _ = self.find_face()
            h, w = map(lambda x: np.diff(x).item(), roi)
            y, x = np.median(np.array(top_left), axis=0).round().astype(int)
            roi = (np.s_[y: y + h, x: x + w],)
        else:
            ROI = {
                'left': (np.s_[:400, :561], np.s_[500:, 100:800]),  # (face, wheel)
                'right': (np.s_[:196, 397:], np.s_[221:, 255:]),
                'body': (np.s_[143:274, 84:433],)  # body holder
            }
            roi = roi or ROI[self.side]

        if test:
            """In test mode load a reference frame and run it through a normalized box filter with
            increasing kernel size.
            """
            idx = (0,)
            ref = self.load_reference_frames(self.side)[idx]
            img = np.empty((n, *ref.shape), dtype=np.uint8)
            kernal_sz = np.unique(np.linspace(0, 15, n, dtype=int))
            for i, k in enumerate(kernal_sz):
                img[i] = ref.copy() if k == 0 else cv2.blur(ref, (k, k))
            if equalize:
                [cv2.equalizeHist(x, x) for x in img]
            if display:
                # Plot blurred images
                f, axes = plt.subplots(1, len(kernal_sz))
                for ax, ig, k in zip(axes, img, kernal_sz):
                    self.imshow(ig, ax=ax, title='Kernal ({0}, {0})'.format(k or 'None'))
                f.suptitle('Reference frame with box filter')
        else:
            # Sub-sample the frame samples
            idx = np.unique(np.linspace(0, len(self.data['frame_samples']) - 1, n, dtype=int))
            img = self.data['frame_samples'][idx]
            if equalize:
                [cv2.equalizeHist(x, x) for x in img]

        # A measure of the sharpness effectively taking the second derivative of the image

        lpc_var = np.empty((min(n, len(img)), len(roi)))
        for i, frame in enumerate(img[::-1]):
            lpc = cv2.Laplacian(frame, cv2.CV_16S, ksize=1)
            lpc_var[i] = [lpc[mask].var() for mask in roi]

        if display:
            # Plot the first sample image
            f = plt.figure()
            gs = f.add_gridspec(len(roi) + 1, 3)
            f.add_subplot(gs[0:len(roi), 0])
            frame = img[0]
            self.imshow(frame, title=f'Frame #{self.frame_samples_idx[idx[0]]}')
            # Plot the ROIs with and without filter
            lpc = cv2.Laplacian(frame, cv2.CV_16S, ksize=1)
            abs_lpc = cv2.convertScaleAbs(lpc)
            for i, r in enumerate(roi):
                f.add_subplot(gs[i, 1])
                self.imshow(frame[r], title=f'ROI #{i + 1}')
                f.add_subplot(gs[i, 2])
                self.imshow(abs_lpc[r], title=f'ROI #{i + 1} - Lapacian filter')
            f.suptitle('Laplacian blur detection')
            # Plot variance over frames
            ax = f.add_subplot(gs[len(roi), :])
            ln = plt.plot(lpc_var)
            [l.set_label(f'ROI #{i + 1}') for i, l in enumerate(ln)]
            ax.axhline(threshold[0], 0, n, linestyle=':', color='r', label='lower threshold')
            ax.set(xlabel='Frame sample', ylabel='Variance of the Laplacian')
            plt.tight_layout()
            plt.legend()

        # Second test is to highpass with dft
        h, w = img.shape[1:]
        cX, cY = w // 2, h // 2
        sz = 60  # Seems to be the magic number for high pass
        mask = np.ones((h, w, 2), bool)
        mask[cY - sz:cY + sz, cX - sz:cX + sz] = False
        filt_mean = np.empty(len(img))
        for i, frame in enumerate(img[::-1]):
            dft = cv2.dft(np.float32(frame), flags=cv2.DFT_COMPLEX_OUTPUT)
            f_shift = np.fft.fftshift(dft) * mask  # Shift & remove low frequencies
            f_ishift = np.fft.ifftshift(f_shift)  # Shift back
            filt_frame = cv2.idft(f_ishift)  # Reconstruct
            filt_frame = cv2.magnitude(filt_frame[..., 0], filt_frame[..., 1])
            # Re-normalize to 8-bits to make threshold simpler
            img_back = cv2.normalize(filt_frame, None, alpha=0, beta=256,
                                     norm_type=cv2.NORM_MINMAX, dtype=cv2.CV_8U)
            filt_mean[i] = np.mean(img_back)
            if i == len(img) - 1 and display:
                # Plot Fourier transforms
                f = plt.figure()
                gs = f.add_gridspec(2, 3)
                self.imshow(img[0], ax=f.add_subplot(gs[0, 0]), title='Original frame')
                dft_shift = np.fft.fftshift(dft)
                magnitude = 20 * np.log(cv2.magnitude(dft_shift[..., 0], dft_shift[..., 1]))
                self.imshow(magnitude, ax=f.add_subplot(gs[0, 1]), title='Magnitude spectrum')
                self.imshow(img_back, ax=f.add_subplot(gs[0, 2]), title='Filtered frame')
                ax = f.add_subplot(gs[1, :])
                ax.plot(filt_mean)
                ax.axhline(threshold[1], 0, n, linestyle=':', color='r', label='lower threshold')
                ax.set(xlabel='Frame sample', ylabel='Mean of filtered frame')
                f.suptitle('Discrete Fourier Transform')
                plt.show()
        passes = np.all(lpc_var > threshold[0]) and np.all(filt_mean > threshold[1])
        return 'PASS' if passes else 'FAIL'

    def find_face(self, roi=None, test=False, metric=cv2.TM_CCOEFF_NORMED, refs=None):
        """Use template matching to find face location in frame
        For the template matching zero-normalized cross-correlation (default) should be more
        robust to exposure (which we're not checking here).  The L2 norm (TM_SQDIFF) should
        also work.  That said, normalizing the histograms works best.

        :param roi: A tuple of indices for the face template in the for ((y1, y2), (x1, x2))
        :returns: (y1, y2), (x1, x2)
        """
        ROI = {
            'left': ((45, 346), (138, 501)),
            'right': ((14, 174), (430, 618)),
            'body': ((141, 272), (90, 339))
        }
        roi = roi or ROI[self.side]
        refs = self.load_reference_frames(self.side) if refs is None else refs

        frames = refs if test else self.data['frame_samples']
        template = refs[0][tuple(slice(*r) for r in roi)]
        top_left = []  # [(x1, y1), ...]
        for frame in frames:
            res = cv2.matchTemplate(frame, template, metric)
            min_val, max_val, min_loc, max_loc = cv2.minMaxLoc(res)
            # If the method is TM_SQDIFF or TM_SQDIFF_NORMED, take minimum
            top_left.append(min_loc if metric < 2 else max_loc)
            # bottom_right = (top_left[0] + w, top_left[1] + h)
        return top_left, roi, template


    @staticmethod
    def load_reference_frames(side):
        """
        Load some reference frames for a given video
        :param side: Video label, e.g. 'left'
        :return: numpy array of frames with the shape (n, h, w)
        """
        file = next(Path(__file__).parent.joinpath('reference').glob(f'frames_{side}.npy'))
        refs = np.load(file)
        return refs

    @staticmethod
    def imshow(frame, ax=None, title=None, **kwargs):
        """plt.imshow with some convenient defaults for greyscale frames"""
        h = ax or plt.gca()
        defaults = {
            'cmap': kwargs.pop('cmap', 'gray'),
            'vmin': kwargs.pop('vmin', 0),
            'vmax': kwargs.pop('vmax', 255)
        }
        h.imshow(frame, **defaults, **kwargs)
        h.set(title=title)
        h.set_axis_off()
        return ax


def run_all_qc(session, update=False, cameras=('left', 'right', 'body'), stream=True, **kwargs):
    """Run QC for all cameras
    Run the camera QC for left, right and body cameras.
    :param session: A session path or eid.
    :param update: If True, QC fields are updated on Alyx.
    :param cameras: A list of camera names to perform QC on.
    :return: dict of CameraCQ objects
    """
    qc = {}
    one = kwargs.pop('one', None)
    for camera in cameras:
        qc[camera] = CameraQC(session, side=camera, stream=stream, one=one)
        qc[camera].run(update=update, **kwargs)
    return qc
