(() => {
  const workspace = document.querySelector("#workspace");
  const videoTemplate = document.querySelector("#tile-template");
  const audioTemplate = document.querySelector("#audio-tile-template");
  const inputTemplate = document.querySelector("#input-tile-template");
  const fullscreen = document.querySelector("#fullscreen");
  const tiles = new Map();
  const interactive = "button,select,input,summary,details,label,.file-controls,.audio-controls";
  let pc = null;
  let ws = null;
  let audioContext = null;
  let streamMap = new Map();
  let negotiatedOutputs = [];
  let negotiatedInputs = [];
  let answerResolve = null;
  let answerReject = null;
  let negotiation = Promise.resolve();
  let zIndex = 1;

  function setStatus(text) {
    document.body.dataset.connection = text;
  }

  function showNotice(text) {
    const notice = document.querySelector("#notice");
    notice.querySelector("p").textContent = text;
    if (!notice.open) notice.showModal();
  }

  // ROS-facing order: video input, audio output, video output, audio input.
  // browser_to_pc is a ROS output; the opposite direction is a ROS input.
  function mediaOrder(item) {
    const rosOutput = item.direction === "browser_to_pc";
    if (!rosOutput && item.media_type === "video") return 0;
    if (rosOutput && item.media_type === "audio") return 1;
    if (rosOutput && item.media_type === "video") return 2;
    return 3;
  }

  function tileSize(count) {
    const gap = 12;
    const width = workspace.clientWidth;
    const height = workspace.clientHeight;
    const columns = Math.max(1, Math.ceil(Math.sqrt(count * width / Math.max(height, 1))));
    return { columns, width: Math.max(290, Math.floor((width - gap * (columns + 1)) / columns)), gap };
  }

  function fittedHeight(item, width) {
    return item.mediaType === "external"
      ? Math.min(Number(item.root.dataset.layoutHeight) || item.root.offsetHeight || 220, workspace.clientHeight)
      : item.mediaType === "audio"
      ? 190
      : item.direction === "browser_to_pc" && !item.hasVideoSize ? 300
      : Math.max(150, Math.round(width / (item.aspectRatio || 16 / 9) + 46));
  }

  function constrainTile(item) {
    if (workspace.hidden) return;
    const root = item.root;
    if (item.maximized && !root.classList.contains("collapsed")) {
      Object.assign(root.style, {
        width: `${workspace.clientWidth}px`, height: `${workspace.clientHeight}px`, left: "0px", top: "0px",
      });
      return;
    }
    const ratio = item.aspectRatio || (item.mediaType === "audio" ? 3 : 16 / 9);
    const maxWidth = Math.max(1, Math.min(workspace.clientWidth, (workspace.clientHeight - 46) * ratio));
    const width = Math.min(root.offsetWidth, maxWidth);
    const preferredHeight = item.freeResize ? root.offsetHeight : fittedHeight(item, width);
    const height = root.classList.contains("collapsed") ? 46 : Math.min(preferredHeight, workspace.clientHeight);
    if (Math.abs(root.offsetWidth - width) > 1) root.style.width = `${width}px`;
    if (Math.abs(root.offsetHeight - height) > 1) root.style.height = `${height}px`;
    root.style.left = `${Math.max(0, Math.min(root.offsetLeft, workspace.clientWidth - width))}px`;
    root.style.top = `${Math.max(0, Math.min(root.offsetTop, workspace.clientHeight - height))}px`;
  }

  function resetLayout() {
    if (workspace.hidden) return;
    const managedRoots = new Set([...tiles.values()].map(item => item.root));
    const externalItems = [...workspace.querySelectorAll(".tile[data-layout-participant]")]
      .filter(root => !managedRoots.has(root))
      .map(root => ({ mediaType: "external", root, preferredWidth: Number(root.dataset.layoutWidth) || 420 }));
    const items = [...tiles.values(), ...externalItems];
    const layout = tileSize(items.length);
    const adaptiveWidth = (item) => item.mediaType === "external"
      ? Math.min(layout.width, item.preferredWidth)
      : item.mediaType === "audio"
      ? layout.width
      : Math.min(layout.width, Math.max(1, (workspace.clientHeight - 46) * (item.aspectRatio || 16 / 9)));
    const rowTops = [];
    const rowHeights = [];
    items.forEach((item, index) => {
      const row = Math.floor(index / layout.columns);
      rowHeights[row] = Math.max(rowHeights[row] || 0, fittedHeight(item, adaptiveWidth(item)));
    });
    rowHeights.forEach((height, row) => {
      rowTops[row] = layout.gap + rowHeights.slice(0, row).reduce((sum, value) => sum + value + layout.gap, 0);
    });
    items.forEach((item, index) => {
      const row = Math.floor(index / layout.columns);
      const left = layout.gap + (index % layout.columns) * (layout.width + layout.gap);
      const top = rowTops[row];
      const width = adaptiveWidth(item);
      const height = fittedHeight(item, width);
      Object.assign(item.root.style, {
        width: `${width}px`, height: `${height}px`, left: `${left}px`, top: `${top}px`, zIndex: `${++zIndex}`,
      });
      item.homeRect = { left, top, width, height };
      item.maximized = false;
      if (item.mediaType === "external") {
        item.root.dataset.layoutHome = JSON.stringify(item.homeRect);
        item.root.dispatchEvent(new CustomEvent("media-card-layout", { detail: item.homeRect }));
      }
    });
  }

  function updateSizeButton(item) {
    if (!item.restore || !item.homeRect) return;
    const adaptive = !item.maximized
      && Math.abs(item.root.offsetWidth - item.homeRect.width) <= 2
      && Math.abs(item.root.offsetHeight - item.homeRect.height) <= 2;
    const mode = adaptive ? "maximize" : "restore";
    const label = adaptive ? "Maximize card" : "Restore adaptive size";
    item.restore.dataset.mode = mode;
    item.restore.title = label;
    item.restore.setAttribute("aria-label", label);
  }

  function restoreSize(item) {
    if (!item.homeRect) return;
    item.maximized = false;
    item.root.style.width = `${item.homeRect.width}px`;
    item.root.style.height = `${item.homeRect.height}px`;
    if (item.preMaxPosition) {
      item.root.style.left = `${item.preMaxPosition.left}px`;
      item.root.style.top = `${item.preMaxPosition.top}px`;
      item.preMaxPosition = null;
    }
    item.root.style.zIndex = `${++zIndex}`;
    constrainTile(item);
    updateSizeButton(item);
  }

  function toggleTileSize(item) {
    if (item.restore.dataset.mode !== "maximize") {
      restoreSize(item);
      return;
    }
    item.preMaxPosition = { left: item.root.offsetLeft, top: item.root.offsetTop };
    item.maximized = true;
    item.root.classList.remove("collapsed");
    Object.assign(item.root.style, {
      width: `${workspace.clientWidth}px`, height: `${workspace.clientHeight}px`, left: "0px", top: "0px", zIndex: `${++zIndex}`,
    });
    updateSizeButton(item);
  }

  function toggleCollapsed(item) {
    const wasCollapsed = item.root.classList.contains("collapsed");
    if (!wasCollapsed) {
      item.expandedWidth = item.root.offsetWidth;
      item.expandedHeight = item.root.offsetHeight;
    }
    item.root.classList.toggle("collapsed", !wasCollapsed);
    if (wasCollapsed) {
      item.root.style.width = `${item.expandedWidth || item.homeRect?.width || 290}px`;
      item.root.style.height = `${item.expandedHeight || fittedHeight(item, item.root.offsetWidth)}px`;
    } else {
      const labelWidth = Math.ceil(item.root.querySelector(".label").scrollWidth);
      item.root.style.width = `${Math.max(120, Math.min(260, labelWidth + 64))}px`;
      item.root.style.height = "46px";
    }
    item.collapse.title = wasCollapsed ? "Collapse card" : "Expand card";
    item.collapse.setAttribute("aria-label", item.collapse.title);
    item.collapse.setAttribute("aria-expanded", String(wasCollapsed));
    constrainTile(item);
  }

  function draggable(root) {
    root.addEventListener("pointerdown", (event) => {
      const rect = root.getBoundingClientRect();
      const onResizeCorner = event.clientX > rect.right - 22 && event.clientY > rect.bottom - 22;
      const xrOutsideTitle = document.body.classList.contains("xr-active") && !event.target.closest(".tile-head");
      if (event.button !== 0 || event.target.closest(interactive) || onResizeCorner || xrOutsideTitle) return;
      event.preventDefault();
      root.style.zIndex = `${++zIndex}`;
      root.classList.add("dragging");
      const startX = event.clientX;
      const startY = event.clientY;
      const left = root.offsetLeft;
      const top = root.offsetTop;
      root.setPointerCapture(event.pointerId);
      const move = (current) => {
        root.style.left = `${Math.max(0, Math.min(workspace.clientWidth - root.offsetWidth, left + current.clientX - startX))}px`;
        root.style.top = `${Math.max(0, Math.min(workspace.clientHeight - root.offsetHeight, top + current.clientY - startY))}px`;
      };
      const stop = () => {
        root.classList.remove("dragging");
        root.removeEventListener("pointermove", move);
        root.removeEventListener("pointerup", stop);
        root.removeEventListener("pointercancel", stop);
      };
      root.addEventListener("pointermove", move);
      root.addEventListener("pointerup", stop);
      root.addEventListener("pointercancel", stop);
    });
  }

  function sendControl(stream, action, value = null) {
    if (ws?.readyState === WebSocket.OPEN) ws.send(JSON.stringify({ type: "stream.control", stream, action, value }));
  }

  function sendTrackState(stream, paused) {
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: paused ? "stream.pause" : "stream.resume", stream }));
    }
  }

  function negotiate() {
    negotiation = negotiation.then(async () => {
      if (pc.getTransceivers().length === 0) {
        setStatus("ready");
        return;
      }
      const answerReady = new Promise((resolve, reject) => {
        answerResolve = resolve;
        answerReject = reject;
      });
      const offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      await new Promise((resolve) => {
        if (pc.iceGatheringState === "complete") return resolve();
        pc.addEventListener("icegatheringstatechange", function changed() {
          if (pc.iceGatheringState === "complete") {
            pc.removeEventListener("icegatheringstatechange", changed);
            resolve();
          }
        });
      });
      ws.send(JSON.stringify({
        type: pc.localDescription.type, sdp: pc.localDescription.sdp,
        defer_mdns: true,
        streams: negotiatedOutputs.map((item) => item.id),
        inputs: negotiatedInputs.map((info) => {
          const item = tiles.get(info.id);
          return { id: info.id, media_type: info.media_type,
            track_id: item?.transceiver?.sender.track?.id || null,
            mid: item?.transceiver?.mid ?? null };
        }),
      }));
      await answerReady;
    });
    return negotiation;
  }

  function formatTime(value) {
    const seconds = Math.max(0, Math.floor(Number(value) || 0));
    return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
  }

  function setPlaybackButton(button, state) {
    const labels = { start: "Start", pause: "Pause", resume: "Resume", replay: "Replay" };
    button.dataset.state = state;
    button.title = labels[state];
    button.setAttribute("aria-label", labels[state]);
  }

  function updateInfoVisibility(item) {
    let selected = 0;
    item.root.querySelectorAll(".info-options input").forEach((input) => {
      if (item.infoFields[input.value]) item.infoFields[input.value].hidden = !input.checked;
      if (input.checked) selected += 1;
    });
    item.root.classList.toggle("info-empty", selected === 0);
  }

  function setupCommonTile(item) {
    item.root.addEventListener("pointerdown", () => { item.root.style.zIndex = `${++zIndex}`; }, { capture: true });
    new ResizeObserver(() => {
      constrainTile(item);
      requestAnimationFrame(() => updateSizeButton(item));
    }).observe(item.root);
    item.root.querySelector(".info-options")?.addEventListener("change", () => updateInfoVisibility(item));
    item.pin?.addEventListener("change", () => item.root.classList.toggle("info-pinned", item.pin.checked));
    item.collapse.addEventListener("click", () => toggleCollapsed(item));
    item.restore?.addEventListener("click", () => toggleTileSize(item));
    workspace.append(item.root);
    draggable(item.root);
    tiles.set(item.info.id, item);
    updateInfoVisibility(item);
    return item;
  }

  function createVideoTile(info) {
    const root = videoTemplate.content.firstElementChild.cloneNode(true);
    const video = root.querySelector("video");
    const canvas = root.querySelector("canvas");
    const pause = root.querySelector(".pause");
    const stop = root.querySelector(".file-stop");
    const restore = root.querySelector(".restore");
    const collapse = root.querySelector(".collapse-toggle");
    const controls = root.querySelector(".file-controls");
    const time = root.querySelector(".time");
    const pin = root.querySelector(".info-pin input");
    const isFile = info.kind === "file";
    const infoFields = {};
    root.querySelectorAll("[data-info]").forEach((node) => { infoFields[node.dataset.info] = node; });
    root.querySelector(".label").textContent = info.label || info.id;
    root.dataset.streamId = info.id;
    root.classList.toggle("file", isFile);
    pause.hidden = !isFile;
    controls.hidden = !isFile;
    const item = {
      mediaType: "video", root, video, canvas, pause, restore, collapse, controls, time,
      stop,
      pin, infoFields, info, track: null, receiver: null, seeking: false, scrubbing: false, seekable: null,
      seekTarget: null, playbackPosition: 0, decodeSample: null, homeRect: null, expandedWidth: null,
      expandedHeight: null, aspectRatio: 16 / 9, hasVideoSize: false,
    };
    pause.addEventListener("click", () => {
      const action = item.info.status === "ended" ? "replay" : item.info.status === "paused" ? "play" : "pause";
      sendControl(info.id, action);
      item.info.status = action === "pause" ? "paused" : "live";
      root.classList.toggle("paused", action === "pause");
      root.classList.remove("ended");
      setPlaybackButton(pause, action === "pause" ? "resume" : "pause");
      video[action === "pause" ? "pause" : "play"]().catch(() => {});
    });
    stop.addEventListener("click", () => {
      sendControl(info.id, "stop");
      item.info.status = "paused";
      root.classList.add("paused");
      root.classList.remove("ended");
      setPlaybackButton(pause, "resume");
      video.pause();
    });
    return setupCommonTile(item);
  }

  function createAudioTile(info) {
    const root = audioTemplate.content.firstElementChild.cloneNode(true);
    const infoFields = {};
    root.querySelectorAll("[data-audio-info]").forEach((node) => { infoFields[node.dataset.audioInfo] = node; });
    root.querySelector(".label").textContent = info.label || info.id;
    infoFields.format.textContent = `${info.sample_rate || 16000} Hz · ${info.channels || 1} ch`;
    root.dataset.streamId = info.id;
    const item = {
      mediaType: "audio", root, canvas: root.querySelector(".audio-waveform"), audio: root.querySelector("audio"),
      collapse: root.querySelector(".collapse-toggle"), restore: root.querySelector(".restore"),
      pause: root.querySelector(".audio-pause"), output: root.querySelector(".audio-output"),
      stop: root.querySelector(".audio-stop"),
      time: root.querySelector(".audio-time"),
      volume: root.querySelector(".audio-volume"), volumeText: root.querySelector(".audio-volume-value"),
      pin: root.querySelector(".info-pin input"), infoFields, info, track: null, receiver: null, analyser: null,
      analyserData: null, audioLevelDb: null, statsSample: null, paused: false, outputEnabled: false,
      elapsed: 0, lastProgressAt: performance.now(), homeRect: null, expandedWidth: null,
      expandedHeight: null, aspectRatio: 3, freeResize: true,
    };
    item.output.addEventListener("click", async () => {
      item.outputEnabled = !item.outputEnabled;
      item.output.setAttribute("aria-pressed", String(item.outputEnabled));
      item.output.title = item.outputEnabled ? "Disable speaker" : "Enable speaker";
      item.audio.muted = !item.outputEnabled;
      if (audioContext?.state === "suspended") await audioContext.resume();
      await item.audio.play();
    });
    item.volume.addEventListener("input", () => {
      item.audio.volume = Number(item.volume.value) / 100;
      item.volumeText.textContent = `${item.volume.value}%`;
    });
    item.pause.addEventListener("click", () => {
      item.paused = !item.paused;
      sendTrackState(info.id, item.paused);
      root.classList.toggle("paused", item.paused);
      setPlaybackButton(item.pause, item.paused ? "resume" : "pause");
      item.lastProgressAt = performance.now();
    });
    item.stop.addEventListener("click", () => {
      item.paused = true;
      item.elapsed = 0;
      item.lastProgressAt = performance.now();
      sendTrackState(info.id, true);
      root.classList.add("paused");
      setPlaybackButton(item.pause, "resume");
      item.time.textContent = "LIVE 0:00";
    });
    return setupCommonTile(item);
  }

  function createMediaTile(info) {
    if (info.direction === "browser_to_pc") return createInputTile(info);
    return info.media_type === "audio" ? createAudioTile(info) : createVideoTile(info);
  }

  function sendInputControl(item, action, value = null, statusValue = null) {
    if (ws?.readyState === WebSocket.OPEN) {
      ws.send(JSON.stringify({ type: "input.control", stream: item.info.id, action, value, status: statusValue || item.state }));
    }
  }

  async function startOwnedInput(item) {
    if (item.start.disabled) return;
    if (item.source.value === "file" && !item.filePicker.files[0]) {
      item.filePicker.click();
      return;
    }
    if (item.stream) await stopInput(item);
    if (ws?.readyState !== WebSocket.OPEN) throw new Error("上行连接尚未建立，请重新连接");
    item.start.disabled = true;
    try {
      await new Promise((resolve, reject) => {
        const timer = setTimeout(() => {
          item.claimResult = null;
          reject(new Error("申请上行卡片超时，请重试"));
        }, 5000);
        item.claimResult = (message) => {
          clearTimeout(timer);
          item.claimResult = null;
          if (message.ok) resolve();
          else {
            const owner = message.client || {};
            reject(new Error(`${item.info.media_type === "video" ? "视频" : "音频"}上行已被 ${owner.id || "其他客户端"}（${owner.browser || "Browser"}）占用，请等待其停止或离线后重试`));
          }
        };
        sendInputControl(item, "claim");
      });
      await startInput(item);
    } catch (error) {
      await stopInput(item);
      throw error;
    } finally {
      item.start.disabled = false;
    }
  }

  function drawInputWaveform(item, schedule = true) {
    if (!item.analyser) return;
    const ratio = devicePixelRatio || 1;
    const width = Math.max(1, Math.round(item.canvas.clientWidth * ratio));
    const height = Math.max(1, Math.round(item.canvas.clientHeight * ratio));
    if (item.canvas.width !== width || item.canvas.height !== height) {
      item.canvas.width = width; item.canvas.height = height;
    }
    item.analyser.getByteTimeDomainData(item.analyserData);
    const context = item.canvas.getContext("2d");
    context.fillStyle = getComputedStyle(item.canvas).backgroundColor;
    context.fillRect(0, 0, width, height);
    context.beginPath();
    item.analyserData.forEach((value, index) => {
      const x = index * width / (item.analyserData.length - 1);
      const y = height / 2 + (value - 128) / 128 * height * .42;
      if (index) context.lineTo(x, y); else context.moveTo(x, y);
    });
    context.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
    context.lineWidth = Math.max(1.5, ratio * 1.5);
    context.stroke();
    item.canvas.xrRefresh = () => drawInputWaveform(item, false);
    if (schedule) item.animation = requestAnimationFrame(() => drawInputWaveform(item));
  }

  function fitInputVideo(item) {
    if (item.info.media_type !== "video" || !item.media.videoWidth || !item.media.videoHeight) return;
    item.aspectRatio = item.media.videoWidth / item.media.videoHeight;
    item.hasVideoSize = true;
    const width = Math.min(
      item.root.offsetWidth,
      workspace.clientWidth,
      Math.max(1, (workspace.clientHeight - 46) * item.aspectRatio),
    );
    const height = fittedHeight(item, width);
    item.root.style.width = `${width}px`;
    item.root.style.height = `${height}px`;
    if (item.homeRect) Object.assign(item.homeRect, { width, height });
    constrainTile(item);
  }

  function fitInputAudioFile(item) {
    if (item.info.media_type !== "audio" || item.source.value !== "file") return;
    const width = Math.min(item.root.offsetWidth, workspace.clientWidth);
    const height = Math.min(190, workspace.clientHeight);
    item.root.style.width = `${width}px`;
    item.root.style.height = `${height}px`;
    if (item.homeRect) Object.assign(item.homeRect, { width, height });
    constrainTile(item);
  }

  async function stopInput(item) {
    if (item.transceiver) {
      if (pc?.signalingState !== "closed" && !item.transceiver.stopped) {
        await item.transceiver.sender.replaceTrack(null);
        item.transceiver.direction = "inactive";
      }
      item.transceiver = null;
    }
    item.stream?.getTracks().forEach((track) => track.stop());
    item.stream = null;
    item.media.pause();
    item.media.removeAttribute("src");
    item.media.srcObject = null;
    if (item.objectUrl) URL.revokeObjectURL(item.objectUrl);
    item.objectUrl = null;
    if (item.animation) cancelAnimationFrame(item.animation);
    item.animation = null;
    item.state = "idle";
    item.root.classList.remove("playing", "paused", "ended");
    item.root.querySelector(".waiting").textContent = "select a source";
    item.stop.disabled = true; item.start.disabled = false;
    item.pcPreview.disabled = true;
    item.pcPreview.setAttribute("aria-pressed", "false");
    item.pcSpeaker.disabled = true;
    item.pcSpeaker.setAttribute("aria-pressed", "false");
    setPlaybackButton(item.start, "start");
    item.time.textContent = "0:00 / 0:00";
    sendInputControl(item, "stop", null, "idle");
  }

  async function setInputPaused(item, paused) {
    if (item.source.value !== "file") {
      item.stream?.getTracks().forEach((track) => { track.enabled = !paused; });
    }
    if (
      !paused && item.state === "paused" && item.source.value === "file"
      && item.info.media_type === "video" && typeof item.media.mozCaptureStream === "function"
      && item.media.readyState >= 1 && Number.isFinite(item.media.currentTime)
    ) {
      // Firefox capture can stall after a long pause; preserve the source track.
      item.media.currentTime = item.media.currentTime;
    }
    await item.media[paused ? "pause" : "play"]();
    item.state = paused ? "paused" : "live";
    item.root.classList.toggle("paused", paused);
    setPlaybackButton(item.start, paused ? "resume" : "pause");
    sendInputControl(item, paused ? "pause" : "resume", null, item.state);
  }

  async function startInput(item) {
    if (!pc) throw new Error("input channel is not connected");
    if (item.stream) await stopInput(item);
    const source = item.source.value;
    let stream;
    if (source === "file") {
      const file = item.filePicker.files[0];
      if (!file) {
        item.filePicker.click();
        return;
      }
      if (!item.objectUrl) {
        item.objectUrl = URL.createObjectURL(file);
        item.media.src = item.objectUrl;
      }
      item.media.loop = true;
      await item.media.play();
      stream = item.media.captureStream?.() || item.media.mozCaptureStream?.();
      if (!stream) throw new Error("this browser cannot stream imported media");
    } else {
      if (!navigator.mediaDevices?.getUserMedia) {
        throw new Error("Device capture requires HTTPS or localhost");
      }
      stream = await navigator.mediaDevices.getUserMedia(
        item.info.media_type === "video"
          ? { video: true, audio: false }
          : { video: false, audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true } }
      );
      item.media.srcObject = stream;
      await item.media.play();
    }
    fitInputVideo(item);
    const track = item.info.media_type === "video" ? stream.getVideoTracks()[0] : stream.getAudioTracks()[0];
    if (!track) throw new Error(`selected source has no ${item.info.media_type} track`);
    if (item.info.media_type === "video" && "contentHint" in track) {
      track.contentHint = "motion";
    }
    item.stream = stream;
    let needsNegotiation = false;
    if (!item.transceiver) {
      item.transceiver = pc.addTransceiver(track, { direction: "sendonly" });
      needsNegotiation = true;
    } else {
      await item.transceiver.sender.replaceTrack(track);
    }
    if (needsNegotiation) await negotiate();
    item.state = "live";
    item.root.classList.add("playing");
    item.root.classList.remove("paused", "ended");
    item.root.querySelector(".waiting").textContent = "waiting for source";
    item.stop.disabled = false; item.start.disabled = false; setPlaybackButton(item.start, "pause");
    item.pcPreview.disabled = false;
    item.pcSpeaker.disabled = !isAudioInput(item);
    if (item.info.media_type === "audio") {
      audioContext ||= new AudioContext();
      if (audioContext.state === "suspended") await audioContext.resume();
      const analyser = audioContext.createAnalyser();
      analyser.fftSize = 2048;
      audioContext.createMediaStreamSource(stream).connect(analyser);
      item.analyser = analyser;
      item.analyserData = new Uint8Array(analyser.fftSize);
      drawInputWaveform(item);
    }
    sendInputControl(item, "start", source, "live");
  }

  function createInputTile(info) {
    const root = inputTemplate.content.firstElementChild.cloneNode(true);
    const isAudio = info.media_type === "audio";
    root.classList.add(isAudio ? "audio-input" : "video-input");
    root.querySelector(".label").textContent = info.label || info.id;
    root.dataset.streamId = info.id;
    const media = root.querySelector(isAudio ? "audio" : "video") || root.querySelector(".input-preview");
    if (isAudio) {
      const audio = document.createElement("audio");
      audio.muted = true; audio.playsInline = true;
      root.append(audio);
    }
    const item = {
      mediaType: isAudio ? "audio" : "video", direction: "browser_to_pc", info, root,
      media: isAudio ? root.querySelector("audio") : media, canvas: root.querySelector(".input-waveform"),
      collapse: root.querySelector(".collapse-toggle"), restore: root.querySelector(".restore"),
      source: root.querySelector(".input-source"), fileButton: root.querySelector(".input-file"),
      filePicker: root.querySelector(".input-file-picker"), start: root.querySelector(".input-start"),
      pause: root.querySelector(".input-start"), stop: root.querySelector(".input-stop"),
      pcPreview: root.querySelector(".input-pc-preview"), pcSpeaker: root.querySelector(".input-pc-speaker"),
      time: root.querySelector(".input-time"),
      stream: null, transceiver: null, state: "idle", aspectRatio: isAudio ? 3 : 16 / 9,
      freeResize: true, hasVideoSize: false,
      homeRect: null, expandedWidth: null, expandedHeight: null,
    };
    item.filePicker.accept = isAudio ? "audio/*" : "video/*";
    item.pcSpeaker.hidden = !isAudio;
    const updateSourceControls = () => {
      const isFile = item.source.value === "file";
      item.fileButton.hidden = !isFile;
      root.querySelectorAll(".file-playback").forEach((control) => { control.hidden = !isFile; });
    };
    item.source.addEventListener("change", async () => {
      try {
        if (item.stream || item.state !== "idle") await stopInput(item);
        else {
          item.media.pause();
          item.media.removeAttribute("src");
          item.media.srcObject = null;
          if (item.objectUrl) URL.revokeObjectURL(item.objectUrl);
          item.objectUrl = null;
        }
      } finally {
        updateSourceControls();
      }
    });
    item.fileButton.hidden = true;
    updateSourceControls();
    item.fileButton.addEventListener("click", () => item.filePicker.click());
    item.filePicker.addEventListener("change", async () => {
      const file = item.filePicker.files[0];
      if (!file) return;
      if (item.stream || item.state !== "idle") await stopInput(item);
      else {
        item.media.pause();
        item.media.srcObject = null;
        if (item.objectUrl) URL.revokeObjectURL(item.objectUrl);
        item.objectUrl = null;
      }
      item.fileButton.textContent = file?.name || "Choose";
      item.objectUrl = file ? URL.createObjectURL(file) : null;
      if (item.objectUrl) {
        item.media.srcObject = null;
        item.media.src = item.objectUrl;
        item.media.load();
      }
    });
    item.start.addEventListener("click", async () => {
      try {
        if (item.state === "idle" || item.state === "ended") {
          await startOwnedInput(item);
          return;
        }
        await setInputPaused(item, item.state !== "paused");
      } catch (error) {
        setStatus(error.message);
        showNotice(error.message);
      }
    });
    const updateFileProgress = () => {
      if (item.source.value !== "file") return;
      const duration = Number.isFinite(item.media.duration) ? item.media.duration : 0;
      item.time.textContent = `${formatTime(item.media.currentTime || 0)} / ${formatTime(duration)}`;
    };
    item.media.addEventListener("loadedmetadata", () => {
      updateFileProgress();
      fitInputVideo(item);
      fitInputAudioFile(item);
    });
    item.media.addEventListener("durationchange", updateFileProgress);
    item.media.addEventListener("timeupdate", updateFileProgress);
    item.media.addEventListener("ended", () => {
      if (item.source.value !== "file" || item.media.loop) return;
      item.state = "ended";
      root.classList.remove("paused");
      root.classList.add("ended");
      setPlaybackButton(item.start, "replay");
      item.start.disabled = false;
      item.pcPreview.setAttribute("aria-pressed", "false");
      item.pcSpeaker.setAttribute("aria-pressed", "false");
      sendInputControl(item, "ended", null, "ended");
      updateFileProgress();
    });
    item.stop.addEventListener("click", () => stopInput(item));
    item.pcPreview.addEventListener("click", () => {
      const enabled = item.pcPreview.getAttribute("aria-pressed") !== "true";
      item.pcPreview.setAttribute("aria-pressed", String(enabled));
      sendInputControl(item, "preview", enabled);
    });
    item.pcSpeaker.addEventListener("click", () => {
      const enabled = item.pcSpeaker.getAttribute("aria-pressed") !== "true";
      item.pcSpeaker.setAttribute("aria-pressed", String(enabled));
      sendInputControl(item, "speaker", enabled);
    });
    return setupCommonTile(item);
  }

  function isAudioInput(item) {
    return item.direction === "browser_to_pc" && item.info.media_type === "audio";
  }

  function renderVideo(item) {
    if (!item.track || item.video.readyState < 2) { requestAnimationFrame(() => renderVideo(item)); return; }
    const width = item.video.videoWidth;
    const height = item.video.videoHeight;
    if (item.canvas.width !== width || item.canvas.height !== height) {
      const firstSize = !item.hasVideoSize;
      const wasAdaptive = item.restore.dataset.mode === "maximize";
      item.canvas.width = width;
      item.canvas.height = height;
      item.aspectRatio = width / height;
      const adaptiveWidth = Math.min(
        item.root.offsetWidth,
        workspace.clientWidth,
        Math.max(1, (workspace.clientHeight - 46) * item.aspectRatio),
      );
      const adaptiveHeight = fittedHeight(item, adaptiveWidth);
      if (item.homeRect) Object.assign(item.homeRect, { width: adaptiveWidth, height: adaptiveHeight });
      if (firstSize || wasAdaptive) {
        item.root.style.width = `${adaptiveWidth}px`;
        item.root.style.height = `${adaptiveHeight}px`;
        updateSizeButton(item);
      }
      item.infoFields.resolution.textContent = `${width}×${height}`;
      if (firstSize) {
        item.hasVideoSize = true;
        clearTimeout(renderVideo.layoutTimer);
        renderVideo.layoutTimer = setTimeout(resetLayout, 80);
      }
    }
    item.canvas.getContext("2d", { alpha: false }).drawImage(item.video, 0, 0, width, height);
    item.root.classList.add("playing");
    requestAnimationFrame(() => renderVideo(item));
  }

  function renderAudio(item, schedule = true) {
    const ratio = window.devicePixelRatio || 1;
    const width = Math.max(1, Math.round(item.canvas.clientWidth * ratio));
    const height = Math.max(1, Math.round(item.canvas.clientHeight * ratio));
    if (item.canvas.width !== width || item.canvas.height !== height) {
      item.canvas.width = width;
      item.canvas.height = height;
    }
    const context = item.canvas.getContext("2d");
    context.fillStyle = getComputedStyle(item.canvas).backgroundColor;
    context.fillRect(0, 0, width, height);
    if (item.analyser && item.analyserData) {
      item.analyser.getByteTimeDomainData(item.analyserData);
      let energy = 0;
      context.beginPath();
      item.analyserData.forEach((value, index) => {
        const sample = (value - 128) / 128;
        energy += sample * sample;
        const x = index * width / (item.analyserData.length - 1);
        const y = height / 2 + sample * height * 0.42;
        if (index === 0) context.moveTo(x, y); else context.lineTo(x, y);
      });
      context.strokeStyle = getComputedStyle(document.documentElement).getPropertyValue("--accent").trim();
      context.lineWidth = Math.max(1.5, ratio * 1.5);
      context.stroke();
      const rms = Math.sqrt(energy / item.analyserData.length);
      item.audioLevelDb = rms > 0 ? Math.max(-96, 20 * Math.log10(rms)) : -96;
      item.infoFields.level.textContent = `level ${item.audioLevelDb.toFixed(1)} dBFS`;
    } else {
      context.strokeStyle = "#506965";
      context.beginPath();
      context.moveTo(0, height / 2);
      context.lineTo(width, height / 2);
      context.stroke();
    }
    item.canvas.xrRefresh = () => renderAudio(item, false);
    if (schedule) requestAnimationFrame(() => renderAudio(item));
  }

  function bindAudioTrack(item, event) {
    item.track = event.track;
    item.receiver = event.receiver;
    item.audio.srcObject = new MediaStream([event.track]);
    item.audio.muted = !item.outputEnabled;
    item.audio.volume = Number(item.volume.value) / 100;
    item.audio.play().catch(() => {});
    audioContext ||= new AudioContext();
    const source = audioContext.createMediaStreamSource(item.audio.srcObject);
    const analyser = audioContext.createAnalyser();
    const silent = audioContext.createGain();
    analyser.fftSize = 2048;
    silent.gain.value = 0;
    source.connect(analyser);
    analyser.connect(silent);
    silent.connect(audioContext.destination);
    item.analyser = analyser;
    item.analyserData = new Uint8Array(analyser.fftSize);
    item.root.classList.add("playing");
    renderAudio(item);
  }

  async function refreshVideoStats(item, now) {
    const date = new Date();
    const pad = (value) => String(value).padStart(2, "0");
    item.infoFields.time.textContent = `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
    const captureFps = Number(item.info.capture_fps) || 0;
    item.infoFields.captureFps.textContent = captureFps ? `capture ${captureFps.toFixed(1)} fps` : "capture -- fps";
    if (!item.receiver) return;
    const stats = await item.receiver.getStats();
    let inbound = null;
    stats.forEach((report) => { if (report.type === "inbound-rtp" && report.kind === "video") inbound = report; });
    if (!inbound) return;
    const frames = inbound.framesDecoded ?? inbound.framesReceived;
    if (!Number.isFinite(frames)) return;
    if (item.decodeSample) {
      const elapsed = (now - item.decodeSample.time) / 1000;
      if (elapsed > 0) item.infoFields.fps.textContent = `decode ${Math.max(0, (frames - item.decodeSample.frames) / elapsed).toFixed(1)} fps`;
    }
    item.decodeSample = { time: now, frames };
    item.infoFields.frames.textContent = `${frames} frames`;
  }

  async function refreshAudioStats(item, now) {
    if (!item.paused) item.elapsed += (now - item.lastProgressAt) / 1000;
    item.lastProgressAt = now;
    item.time.textContent = `LIVE ${formatTime(item.elapsed)}`;
    if (!item.receiver) return;
    const stats = await item.receiver.getStats();
    let inbound = null;
    stats.forEach((report) => { if (report.type === "inbound-rtp" && report.kind === "audio") inbound = report; });
    if (!inbound) return;
    if (item.statsSample) {
      const elapsed = (now - item.statsSample.time) / 1000;
      const bytes = (inbound.bytesReceived || 0) - item.statsSample.bytes;
      if (elapsed > 0 && bytes >= 0) item.infoFields.bitrate.textContent = `${(bytes * 8 / elapsed / 1000).toFixed(1)} kbps`;
    }
    item.statsSample = { time: now, bytes: inbound.bytesReceived || 0 };
    item.infoFields.jitter.textContent = `jitter ${((inbound.jitter || 0) * 1000).toFixed(1)} ms`;
    item.infoFields.loss.textContent = `loss ${inbound.packetsLost || 0}`;
    const emitted = inbound.jitterBufferEmittedCount || 0;
    const delay = emitted ? (inbound.jitterBufferDelay || 0) / emitted * 1000 : 0;
    item.infoFields.delay.textContent = `delay ${delay.toFixed(1)} ms`;
    item.infoFields.codec.textContent = stats.get(inbound.codecId)?.mimeType || "codec --";
  }

  async function refreshStats() {
    const now = performance.now();
    await Promise.all([...tiles.values()].map(async (item) => {
      try {
        if (item.direction === "browser_to_pc") return;
        if (item.mediaType === "audio") await refreshAudioStats(item, now);
        else await refreshVideoStats(item, now);
      } catch (_) {}
    }));
  }

  let connecting = false;

  async function enterWorkspace() {
    if (connecting) return;
    connecting = true;
    const entry = document.querySelector("#entry");
    const retry = entry.querySelector("button");
    entry.hidden = false;
    workspace.hidden = true;
    workspace.inert = true;
    document.querySelector(".corner-actions").hidden = true;
    document.body.dataset.admitted = "false";
    retry.disabled = true;
    entry.querySelector("p").textContent = "正在申请进入…";
    try {
      await connect();
      workspace.hidden = false;
      workspace.inert = false;
      document.querySelector(".corner-actions").hidden = false;
      entry.hidden = true;
      document.body.dataset.admitted = "true";
      document.dispatchEvent(new Event("media-admitted"));
      resetLayout();
    } catch (error) {
      ws?.close();
      pc?.close();
      workspace.hidden = true;
      workspace.inert = true;
      entry.hidden = false;
      entry.querySelector("p").textContent = error.message;
      setStatus(error.message);
    } finally {
      connecting = false;
      retry.disabled = false;
    }
  }

  function requestAdmission() {
    const signal = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/signal?admission=1`);
    ws = signal;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => finish(new Error("入场确认超时，请重试")), 10000);
      function finish(error) {
        clearTimeout(timer);
        signal.onmessage = signal.onerror = signal.onclose = null;
        if (error) { signal.close(); reject(error); }
        else resolve();
      }
      signal.onerror = () => finish(new Error("连接失败，请检查网络后重试"));
      signal.onclose = () => finish(new Error("连接已关闭，请重试"));
      signal.onmessage = (event) => {
        try {
          const message = JSON.parse(event.data);
          if (message.type === "session.accepted") finish();
          else if (message.code === "capacity_full") {
            const users = message.clients.map((client) => `${client.id} (${client.browser})`).join("、");
            finish(new Error(`当前在线 ${message.count}/${message.limit}，暂时无法进入。使用者：${users}。请等待有人离线后重试。`));
          }
        } catch (_) { finish(new Error("入场响应无效，请重试")); }
      };
    });
  }

  async function connect() {
    await Promise.all([...tiles.values()].filter((item) => item.direction === "browser_to_pc" && item.stream).map(stopInput));
    if (pc) pc.close();
    if (ws) ws.close();
    tiles.forEach((item) => item.placeholderTrack?.stop());
    tiles.clear();
    workspace.replaceChildren();
    setStatus("connecting");
    await requestAdmission();
    const response = await fetch("/api/streams", { cache: "no-store" });
    if (!response.ok) throw new Error("加载卡片失败，请重试");
    const payload = await response.json();
    const available = payload.streams
      .map((item, index) => ({ item, index }))
      .sort((left, right) => mediaOrder(left.item) - mediaOrder(right.item) || left.index - right.index)
      .map(({ item }) => item);
    const outputs = available.filter((item) => item.direction !== "browser_to_pc");
    const inputs = available.filter((item) => item.direction === "browser_to_pc");
    negotiatedOutputs = outputs;
    negotiatedInputs = inputs;
    // Layout and canvas drawing need real dimensions, even during negotiation.
    workspace.hidden = false;
    available.forEach((info) => createMediaTile(info));
    resetLayout();
    pc = new RTCPeerConnection({ bundlePolicy: "max-bundle" });
    const videos = outputs.filter((item) => item.media_type !== "audio");
    const audios = outputs.filter((item) => item.media_type === "audio");
    videos.forEach(() => pc.addTransceiver("video", { direction: "recvonly" }));
    audios.forEach(() => pc.addTransceiver("audio", { direction: "recvonly" }));
    // Browser-input transceivers are created only after a real device or file
    // track is selected. Mobile browsers may keep an initially empty m-line
    // inactive even after replaceTrack() and renegotiation.
    pc.ontrack = (event) => {
      const bind = () => {
        const info = streamMap.get(event.transceiver.mid);
        if (!info) return setTimeout(bind, 0);
        const item = tiles.get(info.id);
        if (!item) return;
        if (event.track.kind === "audio") bindAudioTrack(item, event);
        else {
          item.track = event.track;
          item.receiver = event.receiver;
          item.video.srcObject = new MediaStream([event.track]);
          item.video.play().catch(() => {});
          renderVideo(item);
        }
      };
      bind();
    };
    pc.onconnectionstatechange = () => setStatus(pc.connectionState);
    ws.onmessage = async (event) => {
        try {
          const message = JSON.parse(event.data);
          if (message.type === "error" && message.code === "capacity_full") {
            const users = message.clients.map((client) => `${client.id} (${client.browser})`).join(", ");
            const error = new Error(`连接已满 (${message.count}/${message.limit})：${users}`);
            answerReject?.(error);
            answerResolve = null;
            answerReject = null;
            tiles.forEach((item) => {
              item.stream?.getTracks().forEach((track) => track.stop());
              item.placeholderTrack?.stop();
              if (item.direction === "browser_to_pc") item.start.disabled = true;
            });
            pc.onconnectionstatechange = null;
            pc.close();
            setStatus(error.message);
            showNotice(error.message);
            return;
          }
          if (message.type === "input.result") {
            const item = tiles.get(message.stream);
            if (message.action === "claim") {
              item?.claimResult?.(message);
              return;
            }
            if (!message.ok && item) {
              if (message.code === "input_busy") {
                if (message.action === "stop" || message.action === "ended") return;
                const owner = message.client || {};
                const error = `${item.info.media_type === "video" ? "视频" : "音频"}上行已被 ${owner.id || "其他客户端"}（${owner.browser || "Browser"}）占用`;
                if (message.action === "start") await stopInput(item);
                setStatus(error);
                showNotice(error);
                return;
              }
              const button = message.action === "speaker" ? item.pcSpeaker : item.pcPreview;
              button?.setAttribute("aria-pressed", "false");
              setStatus(
                message.action === "speaker"
                  ? `PC audio output unavailable${message.error ? `: ${message.error}` : ""}`
                  : message.url ? `Open PC preview: ${message.url}` : "Start the input before PC View"
              );
            }
            return;
          }
          if (message.type !== "answer") return;
          streamMap = new Map(message.streams.map((item) => [item.mid, item]));
          await pc.setRemoteDescription({ type: message.type, sdp: message.sdp });
          if (message.ice_deferred != null) {
            ws.send(JSON.stringify({ type: "answer.applied", offer_id: message.ice_deferred }));
          }
          answerResolve?.();
          answerResolve = null;
          answerReject = null;
        } catch (error) {
          answerReject?.(error);
          answerResolve = null;
          answerReject = null;
        }
    };
    if (ws.readyState !== WebSocket.OPEN) throw new Error("连接已关闭，请重试");
    negotiation = Promise.resolve();
    await negotiate();
  }

  async function refreshStatuses() {
    if (document.body.dataset.admitted !== "true") return;
    try {
      const streams = (await (await fetch("/api/streams", { cache: "no-store" })).json()).streams;
      streams.forEach((info) => {
        const item = tiles.get(info.id);
        if (!item) return;
        item.info = info;
        if (item.direction === "browser_to_pc") {
          const status = item.stream ? item.state : "idle";
          item.root.classList.toggle("playing", status === "live");
          item.root.classList.toggle("paused", status === "paused");
          item.root.classList.toggle("ended", status === "ended");
          setPlaybackButton(item.start, status === "idle" ? "start" : status === "ended" ? "replay" : status === "paused" ? "resume" : "pause");
          item.pcPreview.setAttribute("aria-pressed", String(Boolean(item.stream && info.preview)));
          item.pcSpeaker.setAttribute("aria-pressed", String(Boolean(item.stream && info.speaker)));
          return;
        }
        if (item.mediaType === "audio") {
          item.root.classList.toggle("playing", info.status === "live");
          return;
        }
        if (info.kind === "file") {
          const playback = info.playback || {};
          const position = playback.position || 0;
          const duration = playback.duration || 0;
          item.seekable = playback.seekable;
          item.playbackPosition = position;
          if (item.seekable === false) {
            item.seeking = false; item.seekTarget = null; item.root.classList.remove("seeking");
          }
          if (item.seeking && item.seekTarget !== null && info.status === "live" && Math.abs(position - item.seekTarget) < 2) {
            item.seeking = false; item.seekTarget = null; item.root.classList.remove("seeking");
          }
          const seeking = item.seekable !== false && (item.seeking || info.status === "seeking");
          const ended = info.status === "ended" && !seeking;
          const paused = info.status === "paused" && !seeking;
          item.root.classList.toggle("seeking", seeking);
          item.root.classList.toggle("ended", ended);
          item.root.classList.toggle("paused", paused);
          setPlaybackButton(item.pause, ended ? "replay" : paused ? "resume" : "pause");
          item.time.textContent = seeking ? `seeking ${formatTime(item.seekTarget || position)}…` : `${formatTime(position)} / ${formatTime(duration)}`;
          if (ended) item.root.querySelector(".waiting").textContent = "ended · click Replay";
        } else {
          item.root.classList.toggle("ended", info.status === "ended");
          item.root.classList.toggle("paused", info.status === "paused");
        }
      });
    } catch (_) {}
  }

  document.addEventListener("pointerdown", (event) => {
    document.querySelectorAll(".info-menu[open]").forEach((menu) => {
      if (!menu.contains(event.target)) menu.removeAttribute("open");
    });
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") document.querySelectorAll(".info-menu[open]").forEach((menu) => menu.removeAttribute("open"));
  });
  document.querySelector("#connect").addEventListener("click", () => enterWorkspace());
  document.querySelector("#layout").addEventListener("click", resetLayout);
  document.addEventListener("media-layout-request", resetLayout);
  fullscreen.addEventListener("click", () => {
    if (document.fullscreenElement) document.exitFullscreen?.();
    else document.documentElement.requestFullscreen?.();
  });
  document.addEventListener("fullscreenchange", () => {
    fullscreen.title = document.fullscreenElement ? "Exit fullscreen" : "Enter fullscreen";
    fullscreen.setAttribute("aria-label", fullscreen.title);
    requestAnimationFrame(() => tiles.forEach(constrainTile));
  });
  window.addEventListener("resize", () => tiles.forEach(constrainTile));
  document.querySelector("#entry button").addEventListener("click", enterWorkspace);
  setInterval(refreshStatuses, 1000);
  setInterval(refreshStats, 500);
  enterWorkspace();
})();
