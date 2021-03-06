import logging
from abc import abstractmethod
from pathlib import Path

import numpy as np

from oneibl.one import ONE
from alf.io import is_session_path, is_uuid_string


# Map for comparing QC outcomes
CRITERIA = {'CRITICAL': 4,
            'FAIL': 3,
            'WARNING': 2,
            'PASS': 1,
            'NOT_SET': 0
            }


class QC:
    """A base class for data quality control"""
    def __init__(self, endpoint_id, one=None, log=None, endpoint='sessions'):
        """
        :param endpoint_id: Eid for endpoint. If using sessions can also be a session path
        :param log: A logging.Logger instance, if None the 'ibllib' logger is used
        :param one: An ONE instance for fetching and setting the QC on Alyx
        :param endpoint: The enpoint name to apply qc to. Default is 'sessions'
        """
        self.one = one or ONE()
        self.log = log or logging.getLogger('ibllib')
        if endpoint == 'sessions':
            self.endpoint = endpoint
            self._set_eid_or_path(endpoint_id)
            self.json = False
        else:
            self.endpoint = endpoint
            self._confirm_endpoint_id(endpoint_id)
            self.json = True

        # Ensure outcome attribute matches Alyx record
        self._outcome = self.update('NOT_SET', namespace='') if self.eid else 'NOT_SET'
        self.log.debug(f'Current QC status is {self.outcome}')

    @abstractmethod
    def run(self):
        """Run the QC tests and return the outcome
        :return: One of "CRITICAL", "FAIL", "WARNING" or "PASS"
        """
        pass

    @abstractmethod
    def load_data(self):
        """Load the data required to compute the QC
        Subclasses may implement this for loading raw data
        """
        pass

    @property
    def outcome(self):
        return self._outcome

    @outcome.setter
    def outcome(self, value):
        value = value.upper()  # Ensure outcome is uppercase
        if value not in CRITERIA:
            raise ValueError('Invalid outcome; must be one of ' + ', '.join(CRITERIA.keys()))
        if CRITERIA[self._outcome] < CRITERIA[value]:
            self._outcome = value

    def _set_eid_or_path(self, session_path_or_eid):
        """Parse a given eID or session path
        If a session UUID is given, resolves and stores the local path and vice versa
        :param session_path_or_eid: A session eid or path
        :return:
        """
        self.eid = None
        if is_uuid_string(str(session_path_or_eid)):
            self.eid = session_path_or_eid
            # Try to set session_path if data is found locally
            self.session_path = self.one.path_from_eid(self.eid)
        elif is_session_path(session_path_or_eid):
            self.session_path = Path(session_path_or_eid)
            if self.one is not None:
                self.eid = self.one.eid_from_path(self.session_path)
                if not self.eid:
                    self.log.warning('Failed to determine eID from session path')
        else:
            self.log.error('Cannot run QC: an experiment uuid or session path is required')
            raise ValueError("'session' must be a valid session path or uuid")

    def _confirm_endpoint_id(self, endpoint_id):
        # Have as read for now since 'list' isn't working
        target_obj = self.one.alyx.rest(self.endpoint, 'read', id=endpoint_id) or None
        if target_obj:
            self.eid = endpoint_id
            json_field = target_obj.get('json')
            if not json_field:
                self.one.alyx.json_field_update(endpoint=self.endpoint, uuid=self.eid,
                                                field_name='json', data={'qc': 'NOT_SET',
                                                                         'extended_qc': {}})
            elif not json_field.get('qc', None):
                self.one.alyx.json_field_update(endpoint=self.endpoint, uuid=self.eid,
                                                field_name='json', data={'qc': 'NOT_SET',
                                                                         'extended_qc': {}})
        else:
            self.log.error('Cannot run QC: endpoint id is not recognised')
            raise ValueError("'endpoint_id' must be a valid uuid")

    def update(self, outcome=None, namespace='experimenter', override=False):
        """Update the qc field in Alyx
        Updates the 'qc' field in Alyx if the new QC outcome is worse than the current value.
        :param outcome: A string; one of "CRITICAL", "FAIL", "WARNING", "PASS" or "NOT_SET"
        :param namespace: The extended QC key specifying the type of QC associated with the outcome
        :param override: If True the QC field is updated even if new value is better than previous
        :return: The current QC outcome str on Alyx

        Example:
            qc = QC('path/to/session')
            qc.update('PASS')  # Update current QC field to 'PASS' if not set
        """
        outcome = outcome or self.outcome
        outcome = outcome.upper()  # Ensure outcome is uppercase
        if outcome not in CRITERIA:
            raise ValueError('Invalid outcome; must be one of ' + ', '.join(CRITERIA.keys()))
        assert self.eid, 'Unable to update Alyx; eID not set'
        if namespace:  # Record in extended qc
            self.update_extended_qc({namespace: outcome})
        current_status = self.one.alyx.rest(self.endpoint, 'read', id=self.eid)['json']['qc'] \
            if self.json else self.one.alyx.rest(self.endpoint, 'read', id=self.eid)['qc']

        if CRITERIA[current_status] < CRITERIA[outcome] or override:
            r = self.one.alyx.json_field_update(endpoint=self.endpoint, uuid=self.eid,
                                                field_name='json', data={'qc': outcome}) \
                if self.json else self.one.alyx.rest(self.endpoint, 'partial_update', id=self.eid,
                                                     data={'qc': outcome})

            current_status = r['qc'].upper()
            assert current_status == outcome, 'Failed to update session QC'
            self.log.info(f'QC field successfully updated to {outcome} for {self.endpoint[:-1]} '
                          f'{self.eid}')
        self._outcome = current_status
        return self.outcome

    def update_extended_qc(self, data):
        """Update the extended_qc field in Alyx
        Subclasses should chain a call to this.
        :param data: a dict of qc tests and their outcomes, typically a value between 0. and 1.
        :return: the updated extended_qc field
        """
        assert self.eid, 'Unable to update Alyx; eID not set'

        # Ensure None instead of NaNs
        for k, v in data.items():
            if (v is not None and not isinstance(v, str)) and np.isnan(v):
                data[k] = None

        if self.json:
            extended_qc = (self.one.alyx.rest(self.endpoint, 'read', id=self.eid)['json']
                           ['extended_qc']) or {}
            extended_qc.update(data)
            extended_qc_dict = {'extended_qc': extended_qc}
            out = self.one.alyx.json_field_update(
                endpoint=self.endpoint, uuid=self.eid, field_name='json', data=extended_qc_dict)
        else:
            extended_qc = self.one.alyx.rest(
                self.endpoint, 'read', id=self.eid)['extended_qc'] or {}
            extended_qc.update(data)
            out = self.one.alyx.json_field_update(
                endpoint=self.endpoint, uuid=self.eid, field_name='extended_qc',
                data=extended_qc)

        self.log.info(f'Extended QC field successfully updated for {self.endpoint[:-1]} '
                      f'{self.eid}')
        return out
