#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import json
import os

from tornado import gen
from mod import get_hardware
from mod.control_chain import ControlChain
from mod.utils import get_plugin_info, get_plugin_control_inputs_and_monitored_outputs

HMI_ADDRESSING_TYPE_LINEAR       = 0
HMI_ADDRESSING_TYPE_BYPASS       = 1
HMI_ADDRESSING_TYPE_TAP_TEMPO    = 2
HMI_ADDRESSING_TYPE_ENUMERATION  = 4|8 # implies scalepoints
HMI_ADDRESSING_TYPE_SCALE_POINTS = 8
HMI_ADDRESSING_TYPE_TRIGGER      = 16
HMI_ADDRESSING_TYPE_TOGGLED      = 32
HMI_ADDRESSING_TYPE_LOGARITHMIC  = 64
HMI_ADDRESSING_TYPE_INTEGER      = 128

HMI_ACTUATOR_TYPE_FOOTSWITCH = 1
HMI_ACTUATOR_TYPE_KNOB       = 2

# Special URI for non-addressed controls
kNullAddressURI = "null"

# Special URIs for midi-learn
kMidiLearnURI = "/midi-learn"
kMidiUnmapURI = "/midi-unmap"
kMidiCustomPrefixURI = "/midi-custom_" # to show current one

# Limits
kMaxAddressableScalepoints = 100

class Addressings(object):
    ADDRESSING_TYPE_NONE = 0
    ADDRESSING_TYPE_HMI  = 1
    ADDRESSING_TYPE_CC   = 2
    ADDRESSING_TYPE_MIDI = 3

    def __init__(self):
        self.init()
        self._task_addressing = None
        self._task_unaddressing = None
        self._task_get_plugin_data = None
        self._task_get_port_value = None

        self.cc = ControlChain('/dev/ttyS3', 115200)
        self.cc.data_update_cb(self.cc_data_update_callback)
        self.cc.dev_descriptor_cb(self.cc_dev_descriptor_callback)

        self.cc_dev_descriptor_callback({'actuators':[{'id':0},{'id':1},{'id':2},{'id':3}],'id':1,'label':''})

    # -----------------------------------------------------------------------------------------------------------------

    # initialize (clear) all addressings
    def init(self):
        mod_hw = get_hardware()

        # 'hmi_addressings' uses a structure like this:
        # "/hmi/knob1": {'addrs': [...], 'idx': 0}
        # so per actuator we get:
        #  - 'addrs': list of addressings
        #  - 'idx'  : currently selected addressing (index)
        if "actuators" in mod_hw.keys():
            self.hmi_addressings = dict((act['uri'], {'addrs': [], 'idx': -1}) for act in mod_hw['actuators'])
        else:
            self.hmi_addressings = {}

        self.cc_addressings = {}
        self.cc_metadata = {}
        self.midi_addressings = {}

        # Store all possible HMI hardcoded values
        self.hmi_hw2uri_map = {}
        self.hmi_uri2hw_map = {}

        for i in range(0, 4):
            knob_hw  = (0, 0, HMI_ACTUATOR_TYPE_KNOB,       i)
            foot_hw  = (0, 0, HMI_ACTUATOR_TYPE_FOOTSWITCH, i)
            knob_uri = "/hmi/knob%i"       % (i+1)
            foot_uri = "/hmi/footswitch%i" % (i+1)

            self.hmi_hw2uri_map[knob_hw]  = knob_uri
            self.hmi_hw2uri_map[foot_hw]  = foot_uri
            self.hmi_uri2hw_map[knob_uri] = knob_hw
            self.hmi_uri2hw_map[foot_uri] = foot_hw

    # -----------------------------------------------------------------------------------------------------------------

    def get_actuators(self):
        mod_hw = get_hardware()
        actuators = mod_hw['actuators'] if 'actuators' in mod_hw.keys() else {}

        for uri, data in self.cc_metadata.items():
            actuators.append({
                'uri': uri,
                'name' : data['name'],
                'modes': data['modes'],
                'steps': data['steps'],
                'max_assigns': data['max_assigns'],
            })

        return actuators

    def get_addressings(self):
        addressings = {}

        # HMI
        for uri, addrs in self.hmi_addressings.items():
            addrs2 = []
            for addr in addrs['addrs']:
                addrs2.append({
                    'instance_id': addr['instance_id'],
                    'portsymbol' : addr['portsymbol'],
                    'label'      : addr['label'],
                    'minimum'    : addr['minimum'],
                    'maximum'    : addr['maximum'],
                    'steps'      : addr['steps'],
                })
            addressings[uri] = addrs2

        # Control Chain
        for uri, addrs in self.cc_addressings.items():
            addrs2 = []
            for addr in addrs:
                addrs2.append({
                    'instance_id': addr['instance_id'],
                    'portsymbol' : addr['portsymbol'],
                    'label'      : addr['label'],
                    'minimum'    : addr['minimum'],
                    'maximum'    : addr['maximum'],
                    'steps'      : addr['steps'],
                })
            addressings[uri] = addrs2

        return addressings

    def get_status(self):
        return {
            'actuators': self.get_actuators(),
            'addressings': self.get_addressings()
        }

    # -----------------------------------------------------------------------------------------------------------------

    @gen.coroutine
    def load(self, bundlepath, instances):
        datafile = os.path.join(bundlepath, "addressings.json")
        if not os.path.exists(datafile):
            return

        with open(datafile, 'r') as fh:
            data = fh.read()
        data = json.loads(data)

        used_actuators = []

        for actuator_uri in data:
            for addr in data[actuator_uri]:
                instance    = addr['instance']
                instance_id = instances[instance]
                plugin_uri  = addr['uri']
                portsymbol  = addr['port']
                curvalue    = self._task_get_port_value(instance, portsymbol)

                self.add(instance_id, plugin_uri, portsymbol, actuator_uri,
                         addr['label'], addr['maximum'], addr['minimum'], addr['steps'], curvalue)

                if actuator_uri not in used_actuators:
                    used_actuators.append(actuator_uri)

        for actuator_uri in used_actuators:
            actuator_type = self.get_actuator_type(actuator_uri)

            if actuator_type == self.ADDRESSING_TYPE_HMI:
                yield gen.Task(self.hmi_load_first, actuator_uri)

            elif actuator_type == self.ADDRESSING_TYPE_CC:
                yield gen.Task(self.cc_load_all, actuator_uri)

            elif actuator_type == self.ADDRESSING_TYPE_MIDI:
                yield gen.Task(self.midi_load_all, actuator_uri)

    def save(self, bundlepath):
        addressings = self.get_addressings()

        with open(os.path.join(bundlepath, "addressings.json"), 'w') as fh:
            json.dump(addressings, fh)

    # -----------------------------------------------------------------------------------------------------------------

    def add(self, instance_id, plugin_uri, portsymbol, actuator_uri, label, maximum, minimum, steps, value):
        actuator_type = self.get_actuator_type(actuator_uri)

        if actuator_type not in (self.ADDRESSING_TYPE_HMI, self.ADDRESSING_TYPE_CC):
            print("ERROR: Trying to address the wrong way, stop!")
            return None

        options = []

        if portsymbol == ":presets":
            value, maximum, options, spreset = self.get_presets_as_options(instance_id)

        elif portsymbol != ":bypass":
            for port_info in get_plugin_control_inputs_and_monitored_outputs(plugin_uri)['inputs']:
                if port_info["symbol"] == portsymbol:
                    break
            else:
                print("ERROR: Trying to address non-existing control port '%s'" % (portsymbol))
                return None

            # useful info about this port
            pprops = port_info["properties"]

            if "enumeration" in pprops and len(port_info["scalePoints"]) > 0:
                options = [(sp["value"], sp["label"]) for sp in port_info["scalePoints"]]

        addressing_data = {
            'actuator_uri': actuator_uri,
            'instance_id' : instance_id,
            'portsymbol'  : portsymbol,
            'label'       : label,
            'value'       : value,
            'maximum'     : maximum,
            'minimum'     : minimum,
            'steps'       : steps,
            'options'     : options,
        }

        # -------------------------------------------------------------------------------------------------------------

        if actuator_type == self.ADDRESSING_TYPE_HMI:
            if portsymbol == ":bypass":
                hmitype = HMI_ADDRESSING_TYPE_BYPASS
                hmiunit = "(none)"

            elif portsymbol == ":presets":
                hmitype = HMI_ADDRESSING_TYPE_SCALE_POINTS|HMI_ADDRESSING_TYPE_ENUMERATION|HMI_ADDRESSING_TYPE_INTEGER
                hmiunit = "(none)"

            else:
                if "toggled" in pprops:
                    hmitype = HMI_ADDRESSING_TYPE_TOGGLED
                elif "integer" in pprops:
                    hmitype = HMI_ADDRESSING_TYPE_INTEGER
                else:
                    hmitype = HMI_ADDRESSING_TYPE_LINEAR

                if "logarithmic" in pprops:
                    hmitype |= HMI_ADDRESSING_TYPE_LOGARITHMIC
                if "trigger" in pprops:
                    hmitype |= HMI_ADDRESSING_TYPE_TRIGGER
                if "tap_tempo" in pprops: # TODO: do not set this prop for knobs
                    hmitype |= HMI_ADDRESSING_TYPE_TAP_TEMPO

                if "enumeration" in pprops and len(port_info["scalePoints"]) > 0:
                    hmitype |= HMI_ADDRESSING_TYPE_ENUMERATION

                hmiunit = port_info["units"]["symbol"] if "symbol" in port_info["units"] else "none"

            # hmi specific
            addressing_data['hmitype'] = hmitype
            addressing_data['hmiunit'] = hmiunit

            addressings = self.hmi_addressings[actuator_uri]
            addressings['idx'] = len(addressings['addrs'])
            addressings['addrs'].append(addressing_data)

        elif actuator_type == self.ADDRESSING_TYPE_CC:
            addressings = self.cc_addressings[actuator_uri]
            addressings.append(addressing_data)

        return addressing_data

    def add_midi(self, instance_id, portsymbol, midichannel, midicontrol, maximum, minimum):
        actuator_uri = self.create_midi_cc_uri(midichannel, midicontrol)

        # NOTE: label, value, steps and options missing, not needed or used for MIDI
        addressing_data = {
            'actuator_uri': actuator_uri,
            'instance_id' : instance_id,
            'portsymbol'  : portsymbol,
            'maximum'     : maximum,
            'minimum'     : minimum,
            # MIDI specific
            'midichannel' : midichannel,
            'midicontrol' : midicontrol,
        }

        if actuator_uri not in self.midi_addressings.keys():
            self.midi_addressings[actuator_uri] = []

        addressings = self.midi_addressings[actuator_uri]
        addressings.append(addressing_data)

        return addressing_data

    def load_addr(self, actuator_uri, addressing_data, callback):
        addressing_data = addressing_data.copy()

        actuator_hw   = actuator_uri
        actuator_type = self.get_actuator_type(actuator_uri)

        if actuator_type == self.ADDRESSING_TYPE_HMI:
            actuator_hw = self.hmi_uri2hw_map[actuator_uri]
            # HMI specific
            addressings = self.hmi_addressings[actuator_uri]
            addressing_data['addrs_idx'] = addressings['idx']+1
            addressing_data['addrs_max'] = len(addressings['addrs'])

        elif actuator_type == self.ADDRESSING_TYPE_CC:
            actuator_hw = self.cc_metadata[actuator_uri]['hw_id']

        self._task_addressing(actuator_type, actuator_hw, addressing_data, callback)

    def remove(self, addressing_data):
        actuator_uri  = addressing_data['actuator_uri']
        actuator_type = self.get_actuator_type(actuator_uri)

        if actuator_type == self.ADDRESSING_TYPE_HMI:
            addressings       = self.hmi_addressings[actuator_uri]
            addressings_addrs = addressings['addrs']
            addressings_idx   = addressings['idx']

            index = addressings_addrs.index(addressing_data)
            addressings_addrs.pop(index)

            if addressings['idx'] == index:
                addressings['idx'] -= 1

        elif actuator_type == self.ADDRESSING_TYPE_CC:
            addressings = self.cc_addressings[actuator_uri]
            addressings.remove(addressing_data)

        elif actuator_type == self.ADDRESSING_TYPE_MIDI:
            addressings = self.midi_addressings[actuator_uri]
            addressings.remove(addressing_data)

    # -----------------------------------------------------------------------------------------------------------------
    # HMI specific functions

    def hmi_load_current(self, actuator_uri, callback):
        actuator_hmi      = self.hmi_uri2hw_map[actuator_uri]
        addressings       = self.hmi_addressings[actuator_uri]
        addressings_addrs = addressings['addrs']
        addressings_idx   = addressings['idx']
        addressings_len   = len(addressings['addrs'])

        if addressings_len == 0:
            callback(False)
            return

        # current addressing data
        addressing = addressings_addrs[addressings_idx]

        data = {
            'instance_id': addressing['instance_id'],
            'portsymbol' : addressing['portsymbol'],
            'label'      : addressing['label'],
            'value'      : addressing['value'],
            'maximum'    : addressing['maximum'],
            'minimum'    : addressing['minimum'],
            'steps'      : addressing['steps'],
            'options'    : addressing['options'],
            # hmi specific
            'hmitype'    : addressing['hmitype'],
            'hmiunit'    : addressing['hmiunit'],
            'addrs_idx'  : addressings_idx+1,
            'addrs_max'  : addressings_len,
        }
        self._task_addressing(self.ADDRESSING_TYPE_HMI, actuator_hmi, data, callback)

    def hmi_load_footswitches(self, callback):
        def footswitch1_callback(ok):
            self.hmi_load_current("/hmi/footswitch2", callback)

        self.hmi_load_current("/hmi/footswitch1", footswitch1_callback)

    def hmi_load_first(self, actuator_uri, callback):
        addressings     = self.hmi_addressings[actuator_uri]
        addressings_len = len(addressings['addrs'])

        if addressings_len == 0:
            callback(False)
            return

        # jump to first addressing
        addressings['idx'] = 0

        # ready to load
        self.hmi_load_current(actuator_uri, callback)

    def hmi_load_next_hw(self, actuator_hw, callback):
        actuator_uri      = self.hmi_hw2uri_map[actuator_hw]
        addressings       = self.hmi_addressings[actuator_uri]
        addressings_addrs = addressings['addrs']
        addressings_len   = len(addressings['addrs'])

        if addressings_len == 0:
            #self.hmi.control_clean(actuator_hmi[0], actuator_hmi[1], actuator_hmi[2], actuator_hmi[3], callback)
            callback(False)
            return

        # jump to next available addressing
        addressings['idx'] = (addressings['idx'] + 1) % addressings_len

        # reload current value
        addressing = addressings_addrs[addressings['idx']]
        addressing['value'] = self._task_get_port_value(addressing['instance_id'], addressing['portsymbol'])

        # ready to load
        self.hmi_load_current(actuator_uri, callback)

    # -----------------------------------------------------------------------------------------------------------------
    # Control Chain specific functions

    @gen.coroutine
    def cc_load_all(self, actuator_uri, callback):
        actuator_cc = self.cc_metadata[actuator_uri]['hw_id']
        addressings = self.cc_addressings[actuator_uri]

        for addressing in addressings:
            data = {
                'instance_id': addressing['instance_id'],
                'portsymbol' : addressing['portsymbol'],
                'label'      : addressing['label'],
                'value'      : addressing['value'],
                'maximum'    : addressing['maximum'],
                'minimum'    : addressing['minimum'],
                'steps'      : addressing['steps'],
                'options'    : addressing['options'],
            }
            yield gen.Task(self._task_addressing, self.ADDRESSING_TYPE_CC, actuator_cc, data)

        callback(True)

    def cc_data_update_callback(self, updates):
        print("cc_data_update_callback", updates)

    def cc_dev_descriptor_callback(self, dev_desc):
        print("cc_dev_descriptor_callback", dev_desc)
        # {
        #  'actuators': [{'id': 0}, {'id': 1}, {'id': 2}, {'id': 3}],
        #  'id': 1,
        #  'label': ''
        # }

        # FIXME: no label
        dev_label = dev_desc['label'] or "footex"
        dev_id    = dev_desc['id']

        for actuator in dev_desc['actuators']:
            actuator_id  = actuator['id']
            actuator_uri = "/cc/%d/%d" % (dev_id, actuator_id)

            if len(dev_desc['actuators']) > 1:
                actuator_name = "%s %d:%d" % (dev_label.title(), dev_id, actuator_id+1),
            else:
                actuator_name = "%s %d" % (dev_label.title(), dev_id),

            self.cc_addressings[actuator_uri] = []
            self.cc_metadata[actuator_uri] = {
                'hw_id': (dev_id, actuator_id),
                'name' : actuator_name,
                'modes': ":trigger:toggled:", # TODO
                'steps': [], # TODO
                'max_assigns': 1, # TODO
            }

        print(self.cc_addressings)
        print(self.cc_metadata)

    # -----------------------------------------------------------------------------------------------------------------
    # MIDI specific functions

    @gen.coroutine
    def midi_load_all(self, actuator_uri, callback):
        addressings = self.midi_addressings[actuator_uri]

        for addressing in addressings:
            # NOTE: label, value, steps and options missing, not needed or used for MIDI
            data = {
                'instance_id': addressing['instance_id'],
                'portsymbol' : addressing['portsymbol'],
                'value'      : addressing['value'],
                'maximum'    : addressing['maximum'],
                'minimum'    : addressing['minimum'],
                # MIDI specific
                'midichannel': addressing['midichannel'],
                'midicontrol': addressing['midicontrol'],
            }
            yield gen.Task(self._task_addressing, self.ADDRESSING_TYPE_MIDI, actuator_uri, data)

        callback(True)

    # -----------------------------------------------------------------------------------------------------------------
    # Utilities

    def create_midi_cc_uri(self, channel, controller):
        return "%sCh.%i_CC#%i" % (kMidiCustomPrefixURI, channel+1, controller)

    def is_hmi_actuator(self, actuator_uri):
        return actuator_uri.startswith("/hmi/")

    def get_actuator_type(self, actuator_uri):
        if actuator_uri.startswith("/hmi/"):
            return self.ADDRESSING_TYPE_HMI
        if actuator_uri.startswith("/cc/"):
            return self.ADDRESSING_TYPE_CC
        if actuator_uri.startswith(kMidiCustomPrefixURI):
            return self.ADDRESSING_TYPE_MIDI
        return self.ADDRESSING_TYPE_NONE

    def get_presets_as_options(self, instance_id):
        pluginData = self._task_get_plugin_data(instance_id)
        presets    = get_plugin_info(pluginData["uri"])['presets']

        value   = 0
        maximum = min(len(presets), kMaxAddressableScalepoints)
        options = []
        handled = False

        # save preset mapping index
        pluginData['mapPresets'] = []

        # safety check
        if len(presets) == 0:
            pluginData['preset'] = ""
            return None

        # if no preset selected yet, we need to force one
        if not pluginData['preset']:
            pluginData['preset'] = presets[0]['uri']
            handled = True

        # save preset list, within a limit
        for i in range(maximum):
            uri = presets[i]['uri']
            pluginData['mapPresets'].append(uri)
            options.append((str(i), presets[i]['label']))
            if handled:
                continue
            if pluginData['preset'] == uri:
                value = i
                handled = True

        # check if selected preset is non-existent
        if not handled and len(presets) == maximum:
            pluginData['mapPresets'] = []
            pluginData['preset'] = ""
            return None

        # handle case of current preset out of limits (>100)
        if pluginData['preset'] not in pluginData['mapPresets']:
            i = value = maximum
            maximum += 1
            pluginData['mapPresets'].append(presets[i]['uri'])
            options.append((str(i), presets[i]['label']))

        return (value, maximum, options, pluginData['preset'])

    # -----------------------------------------------------------------------------------------------------------------
