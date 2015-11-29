import argparse
import alsaaudio as alsa
import json
import Queue
import urllib2
from threading import Thread
import threading
from connect_ffi import ffi, lib

RATE = 44100
CHANNELS = 2
PERIODSIZE = 44100 / 4 # 0.25s
SAMPLESIZE = 2 # 16 bit integer
MAXPERIODS = int(0.5 * RATE / PERIODSIZE) # 0.5s Buffer
count_underruns = 0
underrun_after_play = False

audio_arg_parser = argparse.ArgumentParser(add_help=False)
audio_arg_parser.add_argument('--device', '-D', help='alsa output device', default='default')
audio_arg_parser.add_argument('--mixer', '-m', help='alsa mixer name for volume control', default=alsa.mixers()[0])
args = audio_arg_parser.parse_known_args()[0]

class PlaybackSession:

    def __init__(self):
        self._active = False

    def is_active(self):
        return self._active

    def activate(self):
        self._active = True

    def deactivate(self):
        self._active = False

class AlsaSink:

    def __init__(self, session, args):
        self._lock = threading.Lock()
        self._args = args
        self._session = session
        self._device = None

    def acquire(self):
        if self._session.is_active():
            try:
                pcm = alsa.PCM(
                    type = alsa.PCM_PLAYBACK,
                    mode = alsa.PCM_NORMAL,
		    device = self._args.device)
		    #device = 'softvol')

                pcm.setchannels(CHANNELS)
                pcm.setrate(RATE)
                pcm.setperiodsize(PERIODSIZE)
                pcm.setformat(alsa.PCM_FORMAT_S16_LE)

                self._device = pcm
                print "AlsaSink: device acquired"
            except alsa.ALSAAudioError as error:
                print "Unable to acquire device: ", error
                self.release()


    def release(self):
        if self._session.is_active() and self._device is not None:
            self._lock.acquire()
            try:
                if self._device is not None:
                    self._device.close()
                    self._device = None
                    print "AlsaSink: device released"
            finally:
                self._lock.release()

    def write(self, data):
        if self._session.is_active() and self._device is not None:
            # write is asynchronous, so, we are in race with releasing the device
            self._lock.acquire()
            try:
                if self._device is not None:
                    self._device.write(data)
            except alsa.ALSAAudioError as error:
                print "Ups! Some badness happened: ", error
            finally:
                self._lock.release()

session = PlaybackSession()
device = AlsaSink(session, args)
mixer = alsa.Mixer(args.mixer)

def userdata_wrapper(f):
    def inner(*args):
        assert len(args) > 0
        self = ffi.from_handle(args[-1])
        return f(self, *args[:-1])
    return inner

#Error callbacks
@ffi.callback('void(SpError error, void *userdata)')
def error_callback(error, userdata):
    print "error_callback: {}".format(error)

#Connection callbacks
@ffi.callback('void(SpConnectionNotify type, void *userdata)')
@userdata_wrapper
def connection_notify(self, type):
    if type == lib.kSpConnectionNotifyLoggedIn:
        print "kSpConnectionNotifyLoggedIn"
    elif type == lib.kSpConnectionNotifyLoggedOut:
        print "kSpConnectionNotifyLoggedOut"
    elif type == lib.kSpConnectionNotifyTemporaryError:
        print "kSpConnectionNotifyTemporaryError"
    else:
        print "UNKNOWN ConnectionNotify {}".format(type)

@ffi.callback('void(const char *blob, void *userdata)')
@userdata_wrapper
def connection_new_credentials(self, blob):
    print ffi.string(blob)
    self.credentials['blob'] = ffi.string(blob)

    with open(self.args.credentials, 'w') as f:
        f.write(json.dumps(self.credentials))

#Debug callbacks
@ffi.callback('void(const char *msg, void *userdata)')
@userdata_wrapper
def debug_message(self, msg):
    print ffi.string(msg)
    global count_underruns
    global underrun_after_play
    if "Requesting Bytes" in ffi.string(msg):
	count_underruns = 0
	underrun_after_play = True
    elif "WARNING: Underrun" in ffi.string(msg):
	count_underruns = count_underruns+1
	if count_underruns > 5 and underrun_after_play:
		after_playing()
        	device.release()
		count_underruns = 0
		underrun_after_play = False
   	else:
		underrun_after_play = False

#Playback callbacks
@ffi.callback('void(SpPlaybackNotify type, void *userdata)')
@userdata_wrapper
def playback_notify(self, type):
    if type == lib.kSpPlaybackNotifyPlay:
        print "kSpPlaybackNotifyPlay"
	device.acquire()
    elif type == lib.kSpPlaybackNotifyPause:
        print "kSpPlaybackNotifyPause"
	after_playing()
        device.release()
    elif type == lib.kSpPlaybackNotifyTrackChanged:
        print "kSpPlaybackNotifyTrackChanged"
    elif type == lib.kSpPlaybackNotifyNext:
        print "kSpPlaybackNotifyNext"
    elif type == lib.kSpPlaybackNotifyPrev:
        print "kSpPlaybackNotifyPrev"
    elif type == lib.kSpPlaybackNotifyShuffleEnabled:
        print "kSpPlaybackNotifyShuffleEnabled"
    elif type == lib.kSpPlaybackNotifyShuffleDisabled:
        print "kSpPlaybackNotifyShuffleDisabled"
    elif type == lib.kSpPlaybackNotifyRepeatEnabled:
        print "kSpPlaybackNotifyRepeatEnabled"
    elif type == lib.kSpPlaybackNotifyRepeatDisabled:
        print "kSpPlaybackNotifyRepeatDisabled"
    elif type == lib.kSpPlaybackNotifyBecameActive:
        print "kSpPlaybackNotifyBecameActive"
	before_playing()
        session.activate()
    elif type == lib.kSpPlaybackNotifyBecameInactive:
        print "kSpPlaybackNotifyBecameInactive"
	after_playing()
        device.release()
        session.deactivate()
    elif type == lib.kSpPlaybackNotifyPlayTokenLost:
        print "kSpPlaybackNotifyPlayTokenLost"
	after_playing()
    elif type == lib.kSpPlaybackEventAudioFlush:
        print "kSpPlaybackEventAudioFlush"
    	before_playing()
    else:
        print "UNKNOWN PlaybackNotify {}".format(type)

def playback_thread(q):
    while True:
        data = q.get()
        device.write(data)
        q.task_done()

audio_queue = Queue.Queue(maxsize=MAXPERIODS)
pending_data = str()

def playback_setup():
    t = Thread(args=(audio_queue,), target=playback_thread)
    t.daemon = True
    t.start()

@ffi.callback('uint32_t(const void *data, uint32_t num_samples, SpSampleFormat *format, uint32_t *pending, void *userdata)')
@userdata_wrapper
def playback_data(self, data, num_samples, format, pending):
    global pending_data

    # Make sure we don't pass incomplete frames to alsa
    num_samples -= num_samples % CHANNELS

    buf = pending_data + ffi.buffer(data, num_samples * SAMPLESIZE)[:]

    try:
        total = 0
        while len(buf) >= PERIODSIZE * CHANNELS * SAMPLESIZE:
            audio_queue.put(buf[:PERIODSIZE * CHANNELS * SAMPLESIZE], block=False)
            buf = buf[PERIODSIZE * CHANNELS * SAMPLESIZE:]
            total += PERIODSIZE * CHANNELS

        pending_data = buf
        return num_samples
    except Queue.Full:
        return total
    finally:
        pending[0] = audio_queue.qsize() * PERIODSIZE * CHANNELS

@ffi.callback('void(uint32_t millis, void *userdata)')
@userdata_wrapper
def playback_seek(self, millis):
    print "playback_seek: {}".format(millis)

@ffi.callback('void(uint16_t volume, void *userdata)')
@userdata_wrapper
def playback_volume(self, volume):
    print "playback_volume: {}".format(volume)
    #Better volume scaling
    procent_volume = int(volume / 655.35)
    fixed_volume = 85
    if procent_volume >= 40:
	fixed_volume = procent_volume - 15 + (100 - procent_volume)/2
    elif procent_volume < 40:
   	fixed_volume = procent_volume * 1.375
    mixer.setvolume(int(fixed_volume))

connection_callbacks = ffi.new('SpConnectionCallbacks *', [
    connection_notify,
    connection_new_credentials
])

debug_callbacks = ffi.new('SpDebugCallbacks *', [
    debug_message
])

playback_callbacks = ffi.new('SpPlaybackCallbacks *', [
    playback_notify,
    playback_data,
    playback_seek,
    playback_volume
])

def before_playing():
	#You can put some stuff here tbd before playback starts
	#urllib2.urlopen("http://192.168.1.210/cgi-bin/air_an.cgi").read()
	
def after_playing():
	#You can put some stuff here tbd after playback stops
	#urllib2.urlopen("http://192.168.1.210/cgi-bin/air_aus.cgi").read()
