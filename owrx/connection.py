from owrx.config import PropertyManager
from owrx.source import DspManager, CpuUsageThread, SdrService, ClientRegistry
from owrx.feature import FeatureDetector
import json

import logging
logger = logging.getLogger(__name__)

class OpenWebRxClient(object):
    config_keys = ["waterfall_colors", "waterfall_min_level", "waterfall_max_level",
                   "waterfall_auto_level_margin", "lfo_offset", "samp_rate", "fft_size", "fft_fps",
                   "audio_compression", "fft_compression", "max_clients", "start_mod",
                   "client_audio_buffer_size", "start_freq", "center_freq", "mathbox_waterfall_colors",
                   "mathbox_waterfall_history_length", "mathbox_waterfall_frequency_resolution"]
    def __init__(self, conn):
        self.conn = conn

        ClientRegistry.getSharedInstance().addClient(self)

        self.dsp = None
        self.sdr = None
        self.configProps = None

        pm = PropertyManager.getSharedInstance()

        self.setSdr()

        # send receiver info
        receiver_keys = ["receiver_name", "receiver_location", "receiver_qra", "receiver_asl",  "receiver_gps",
                         "photo_title", "photo_desc"]
        receiver_details = dict((key, pm.getPropertyValue(key)) for key in receiver_keys)
        self.write_receiver_details(receiver_details)

        profiles = [{"name": s.getName() + " " + p["name"], "id":sid + "|" + pid} for (sid, s) in SdrService.getSources().items() for (pid, p) in s.getProfiles().items()]
        self.write_profiles(profiles)

        features = FeatureDetector().feature_availability()
        self.write_features(features)

        CpuUsageThread.getSharedInstance().add_client(self)

    def sendConfig(self, key, value):
        config = dict((key, self.configProps[key]) for key in OpenWebRxClient.config_keys)
        # TODO mathematical properties? hmmmm
        config["start_offset_freq"] = self.configProps["start_freq"] - self.configProps["center_freq"]
        self.write_config(config)
    def setSdr(self, id = None):
        next = SdrService.getSource(id)
        if (next == self.sdr):
            return

        self.stopDsp()

        if self.configProps is not None:
            self.configProps.unwire(self.sendConfig)

        self.sdr = next

        # send initial config
        self.configProps = self.sdr.getProps().collect(*OpenWebRxClient.config_keys).defaults(PropertyManager.getSharedInstance())

        self.configProps.wire(self.sendConfig)
        self.sendConfig(None, None)

        self.sdr.addSpectrumClient(self)

    def startDsp(self):
        if self.dsp is None:
            self.dsp = DspManager(self, self.sdr)
            self.dsp.start()

    def close(self):
        self.stopDsp()
        CpuUsageThread.getSharedInstance().remove_client(self)
        ClientRegistry.getSharedInstance().removeClient(self)
        self.conn.close()
        logger.debug("connection closed")

    def stopDsp(self):
        if self.dsp is not None:
            self.dsp.stop()
            self.dsp = None
        if self.sdr is not None:
            self.sdr.removeSpectrumClient(self)

    def setParams(self, params):
        # only the keys in the protected property manager can be overridden from the web
        protected = self.sdr.getProps().collect("samp_rate", "center_freq", "rf_gain", "type") \
            .defaults(PropertyManager.getSharedInstance())
        for key, value in params.items():
            protected[key] = value

    def setDspProperties(self, params):
        for key, value in params.items():
            self.dsp.setProperty(key, value)

    def protected_send(self, data):
        try:
            self.conn.send(data)
        # these exception happen when the socket is closed
        except OSError:
            self.close()
        except ValueError:
            self.close()

    def write_spectrum_data(self, data):
        self.protected_send(bytes([0x01]) + data)
    def write_dsp_data(self, data):
        self.protected_send(bytes([0x02]) + data)
    def write_s_meter_level(self, level):
        self.protected_send({"type":"smeter","value":level})
    def write_cpu_usage(self, usage):
        self.protected_send({"type":"cpuusage","value":usage})
    def write_clients(self, clients):
        self.protected_send({"type":"clients","value":clients})
    def write_secondary_fft(self, data):
        self.protected_send(bytes([0x03]) + data)
    def write_secondary_demod(self, data):
        self.protected_send(bytes([0x04]) + data)
    def write_secondary_dsp_config(self, cfg):
        self.protected_send({"type":"secondary_config", "value":cfg})
    def write_config(self, cfg):
        self.protected_send({"type":"config","value":cfg})
    def write_receiver_details(self, details):
        self.protected_send({"type":"receiver_details","value":details})
    def write_profiles(self, profiles):
        self.protected_send({"type":"profiles","value":profiles})
    def write_features(self, features):
        self.protected_send({"type":"features","value":features})

class WebSocketMessageHandler(object):
    def __init__(self):
        self.handshake = None
        self.client = None
        self.dsp = None

    def handleTextMessage(self, conn, message):
        if (message[:16] == "SERVER DE CLIENT"):
            # maybe put some more info in there? nothing to store yet.
            self.handshake = "completed"
            logger.debug("client connection intitialized")

            self.client = OpenWebRxClient(conn)

            return

        if not self.handshake:
            logger.warning("not answering client request since handshake is not complete")
            return

        try:
            message = json.loads(message)
            if "type" in message:
                if message["type"] == "dspcontrol":
                    if "action" in message and message["action"] == "start":
                        self.client.startDsp()

                    if "params" in message:
                        params = message["params"]
                        self.client.setDspProperties(params)

                if message["type"] == "config":
                    if "params" in message:
                        self.client.setParams(message["params"])
                if message["type"] == "setsdr":
                    if "params" in message:
                        self.client.setSdr(message["params"]["sdr"])
                if message["type"] == "selectprofile":
                    if "params" in message and "profile" in message["params"]:
                        profile = message["params"]["profile"].split("|")
                        self.client.setSdr(profile[0])
                        self.client.sdr.activateProfile(profile[1])
            else:
                logger.warning("received message without type: {0}".format(message))

        except json.JSONDecodeError:
            logger.warning("message is not json: {0}".format(message))

    def handleBinaryMessage(self, conn, data):
        logger.error("unsupported binary message, discarding")

    def handleClose(self, conn):
        if self.client:
            self.client.close()
