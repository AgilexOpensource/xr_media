from pathlib import Path
import shutil
import subprocess
import unittest


@unittest.skipUnless(shutil.which("node"), "Node.js is required for browser logic tests")
class BrowserNegotiationTest(unittest.TestCase):
    def test_file_pause_does_not_disable_capture_tracks(self):
        client = Path(__file__).resolve().parents[1] / "static" / "client.js"
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('  async function setInputPaused(item, paused) {');
assert.notEqual(start, -1);
const helper = source.slice(start, source.indexOf('\n  }\n', start) + '\n  }\n'.length);
async function scenario(sourceKind, mediaKind, firefox) {
  const controls = [];
  const calls = [];
  const changes = [];
  const track = {id: 'original-track', kind: mediaKind, _enabled: true,
    get enabled() {return this._enabled;},
    set enabled(value) {changes.push(value); this._enabled = value;},
  };
  const media = {
    _currentTime: 42, src: 'blob:original', paused: false, readyState: 4,
    get currentTime() {return this._currentTime;},
    set currentTime(value) {calls.push('seek'); this._currentTime = value;},
    pause() {calls.push('pause'); this.paused = true;},
    async play() {calls.push('play'); this.paused = false;},
  };
  if (firefox) media.mozCaptureStream = () => {throw new Error('must not recapture');};
  const item = {info: {media_type: mediaKind}, source: {value: sourceKind}, media, stream: {getTracks: () => [track]},
    transceiver: {sender: {track}}, state: 'live',
    root: {classList: {toggle() {}}}, start: {},
  };
  const context = {setPlaybackButton() {},
    sendInputControl(item, action, value, state) {controls.push([action, state]);},
  };
  vm.createContext(context);
  vm.runInContext(helper, context);
  for (let cycle = 0; cycle < 3; cycle++) {
    await context.setInputPaused(item, true);
    assert.equal(item.state, 'paused');
    assert.equal(media.paused, true);
    assert.equal(track.enabled, sourceKind === 'file');
    await context.setInputPaused(item, false);
    assert.equal(item.state, 'live');
    assert.equal(media.paused, false);
    assert.equal(track.enabled, true);
    assert.equal(item.transceiver.sender.track, track);
    assert.equal(media.currentTime, 42);
    assert.equal(media.src, 'blob:original');
  }
  assert.deepEqual(changes, sourceKind === 'file' ? [] : [false, true, false, true, false, true]);
  const expectedCycle = firefox && sourceKind === 'file' && mediaKind === 'video'
    ? ['pause', 'seek', 'play'] : ['pause', 'play'];
  assert.deepEqual(calls, Array.from({length: 3}, () => expectedCycle).flat());
  assert.deepEqual(controls, Array.from({length: 3}, () => [['pause', 'paused'], ['resume', 'live']]).flat());
}
(async () => {
  for (const sourceKind of ['file', 'device']) {
    for (const mediaKind of ['video', 'audio']) {
      for (const firefox of [false, true]) await scenario(sourceKind, mediaKind, firefox);
    }
  }
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(client)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_reconnect_cleans_inputs_before_closing_peer(self):
        client = Path(__file__).resolve().parents[1] / "static" / "client.js"
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name, args) {
  const start = source.indexOf(`  async function ${name}(${args}) {`);
  assert.notEqual(start, -1);
  return source.slice(start, source.indexOf('\n  }\n', start) + '\n  }\n'.length);
}
async function scenario(closed, retired) {
  const events = [];
  const reachedFetch = new Error('cleanup completed');
  const peer = {signalingState: closed ? 'closed' : 'stable', close() {
    events.push('close');
    this.signalingState = 'closed';
  }};
  const socket = {close() {events.push('socket-close');}};
  const button = () => ({setAttribute() {}});
  const item = {
    direction: 'browser_to_pc',
    transceiver: {stopped: retired, direction: 'sendonly', sender: {
      async replaceTrack(track) {
        if (peer.signalingState === 'closed' || retired) throw new Error('InvalidStateError');
        assert.equal(track, null);
        events.push('detach');
      },
    }},
    stream: {getTracks: () => [{stop() {events.push('track-stop');}}]},
    media: {pause() {}, removeAttribute() {}},
    root: {classList: {remove() {}}, querySelector: () => ({})},
    stop: button(), start: button(), pcPreview: button(), pcSpeaker: button(), time: {},
  };
  const context = {
    pc: peer, ws: socket, tiles: new Map([['input', item]]),
    workspace: {replaceChildren() {}}, setStatus() {}, setPlaybackButton() {},
    sendInputControl() {events.push('stop-control');},
    async requestAdmission() {events.push('admission');},
    async fetch() {throw reachedFetch;},
  };
  vm.createContext(context);
  vm.runInContext(extract('stopInput', 'item') + extract('connect', ''), context);
  await assert.rejects(context.connect(), error => error === reachedFetch);
  assert.equal(item.stream, null);
  assert.equal(item.transceiver, null);
  assert.equal(context.tiles.size, 0);
  assert.equal(events.includes('detach'), !closed && !retired);
  assert.ok(events.indexOf('track-stop') < events.indexOf('close'));
  assert.ok(events.indexOf('stop-control') < events.indexOf('socket-close'));
  assert.ok(events.indexOf('socket-close') < events.indexOf('admission'));
}
(async () => {
  await scenario(false, false);
  await scenario(true, false);
  await scenario(false, true);
})().catch(error => {console.error(error); process.exitCode = 1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(client)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_mdns_ack_waits_for_successful_remote_description(self):
        client = Path(__file__).resolve().parents[1] / "static" / "client.js"
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('    ws.onmessage = async (event) => {');
assert.notEqual(start, -1);
const handler = source.slice(start, source.indexOf('\n    };', start) + '\n    };'.length);
async function scenario(deferred, fail) {
  const sent = [];
  let release;
  let resolved = false;
  let rejected = false;
  const applied = new Promise(resolve => { release = resolve; });
  const context = {
    streamMap: new Map(),
    ws: {send(payload) { sent.push(JSON.parse(payload)); }},
    pc: {async setRemoteDescription() {
      await applied;
      if (fail) throw new Error('invalid answer');
    }},
    answerResolve() { resolved = true; },
    answerReject() { rejected = true; },
  };
  vm.createContext(context);
  vm.runInContext(handler, context);
  const pending = context.ws.onmessage({data: JSON.stringify({
    type: 'answer', sdp: 'test', streams: [], ice_deferred: deferred,
  })});
  await Promise.resolve();
  assert.equal(sent.length, 0);
  assert.equal(resolved, false);
  release();
  await pending;
  assert.equal(resolved, !fail);
  assert.equal(rejected, fail);
  assert.deepEqual(sent, !fail && deferred != null
    ? [{type: 'answer.applied', offer_id: deferred}] : []);
}
(async () => {
  await scenario(3, false);
  await scenario(null, false);
  await scenario(3, true);
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(client)],
            capture_output=True, text=True, timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_empty_connection_does_not_block_first_media_offer(self):
        client = Path(__file__).resolve().parents[1] / "static" / "client.js"
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
const start = source.indexOf('  function negotiate() {');
assert.notEqual(start, -1);
const end = source.indexOf('\n  }\n', start) + '\n  }\n'.length;
const negotiateSource = source.slice(start, end);
async function scenario(kind, initiallyEmpty) {
  let offers = 0;
  let sent = 0;
  let media = !initiallyEmpty;
  const context = {
    negotiation: Promise.resolve(),
    negotiatedOutputs: initiallyEmpty ? [] : [{id: 'output'}],
    negotiatedInputs: [{id: 'input', media_type: kind}],
    tiles: new Map([['input', {transceiver: {mid: 'custom-mid', sender: {track: {id: 'browser-track'}}}}]]),
    setStatus() {},
    pc: {
      getTransceivers: () => media ? [{kind}] : [],
      iceGatheringState: 'new',
      async createOffer() { offers++; return {type: 'offer', sdp: media ? `m=${kind}` : ''}; },
      async setLocalDescription(offer) {
        this.localDescription = offer;
        this.iceGatheringState = media ? 'complete' : 'new';
      },
      addEventListener() {},
    },
    ws: {send(payload) {
      sent++;
      assert.equal(JSON.parse(payload).sdp, `m=${kind}`);
      assert.equal(JSON.parse(payload).defer_mdns, true);
      assert.equal(JSON.parse(payload).inputs[0].mid, 'custom-mid');
      assert.equal(JSON.parse(payload).inputs[0].track_id, 'browser-track');
      context.answerResolve();
    }},
  };
  vm.createContext(context);
  vm.runInContext(negotiateSource, context);
  async function bounded(promise) {
    let timer;
    try {
      await Promise.race([promise, new Promise((resolve, reject) => {
        timer = setTimeout(() => reject(new Error('negotiation queue blocked')), 150);
      })]);
    } finally { clearTimeout(timer); }
  }
  if(initiallyEmpty) {
    await bounded(context.negotiate());
    assert.equal(offers, 0);
    assert.equal(sent, 0);
    media = true;
  }
  await bounded(context.negotiate());
  assert.equal(offers, 1);
  assert.equal(sent, 1);
  await bounded(context.negotiate());
  assert.equal(sent, 2);
}
(async () => {
  for(const kind of ['audio', 'video']) {
    await scenario(kind, true);
    await scenario(kind, false);
  }
})().catch(error => { console.error(error); process.exitCode = 1; });
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(client)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_stopped_input_gets_new_transceiver_on_restart(self):
        client = Path(__file__).resolve().parents[1] / "static" / "client.js"
        script = r"""
const assert = require('node:assert/strict');
const fs = require('node:fs');
const vm = require('node:vm');
const source = fs.readFileSync(process.argv[1], 'utf8');
function extract(name) {
  const start = source.indexOf(`  async function ${name}(item) {`);
  assert.notEqual(start, -1);
  return source.slice(start, source.indexOf('\n  }\n', start) + '\n  }\n'.length);
}
async function scenario(kind) {
  let offers = 0;
  let stopped = 0;
  const transceivers = [];
  const controls = [];
  const context = {
    pc: {close() {throw new Error('restart must preserve the shared peer');}, addTransceiver(track, options) {
      const transceiver = {direction: options.direction,
        sender: {track, async replaceTrack(replacement) {this.track = replacement;}}};
      transceivers.push(transceiver);
      return transceiver;
    }},
    navigator: {mediaDevices: {async getUserMedia() {
      const track = {kind, stop() {stopped++;}};
      return {getTracks: () => [track], getVideoTracks: () => [track], getAudioTracks: () => [track]};
    }}},
    negotiate: async () => {offers++;},
    fitInputVideo() {}, setPlaybackButton() {}, drawInputWaveform() {},
    isAudioInput: item => item.info.media_type === 'audio',
    sendInputControl(item, action) {controls.push(action);},
    audioContext: {state: 'running', createAnalyser: () => ({}),
      createMediaStreamSource: () => ({connect() {}})},
  };
  const button = () => ({setAttribute() {}});
  const item = {
    info: {media_type: kind}, source: {value: 'device'},
    media: {async play() {}, pause() {}, removeAttribute() {}},
    root: {classList: {add() {}, remove() {}}, querySelector: () => ({})},
    stop: button(), start: button(), pcPreview: button(), pcSpeaker: button(), time: {},
    transceiver: null, stream: null,
  };
  vm.createContext(context);
  vm.runInContext(extract('stopInput') + extract('startInput'), context);
  for(let cycle = 0; cycle < 3; cycle++) {
    await context.startInput(item);
    assert.equal(offers, cycle + 1);
    assert.equal(transceivers.length, cycle + 1);
    assert.equal(item.transceiver.direction, 'sendonly');
    const retired = item.transceiver;
    await context.stopInput(item);
    assert.equal(retired.direction, 'inactive');
    assert.equal(retired.sender.track, null);
    assert.equal(item.transceiver, null);
    assert.equal(item.stream, null);
  }
  assert.equal(stopped, 3);
  assert.deepEqual(controls, ['start', 'stop', 'start', 'stop', 'start', 'stop']);
}
(async () => {await scenario('video'); await scenario('audio');})()
  .catch(error => {console.error(error); process.exitCode = 1;});
"""
        result = subprocess.run(
            [shutil.which("node"), "-e", script, str(client)],
            capture_output=True,
            text=True,
            timeout=10,
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
