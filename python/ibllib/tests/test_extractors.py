import unittest
from pathlib import Path
import numpy as np

from ibllib.io import raw_data_loaders as loaders
from alf.extractors import training_trials


class TestExtractTrialData(unittest.TestCase):

    def setUp(self):
        self.session_path = Path(__file__).parent.joinpath('data')
        self.data = loaders.load_data(self.session_path)

    def test_stimOn_times(self):
        st = training_trials.get_stimOn_times('', save=False, data=self.data)
        self.assertTrue(isinstance(st, np.ndarray))

    def test_encoder_positions_duds(self):
        dy = loaders.load_encoder_positions(self.session_path)
        self.assertTrue(dy.shape[0] == 2)
