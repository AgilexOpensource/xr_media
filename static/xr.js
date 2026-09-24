import { SpatialCardUI, SpatialXrOverlay, createXrRenderer } from "/xr/vr-ui.js?v=20260921-2";

// Do not initialize XR cards until the media server grants a seat.
if (document.body.dataset.admitted !== "true") {
  await new Promise(resolve => document.addEventListener("media-admitted", resolve, { once: true }));
}

const workspace = document.querySelector("#workspace");
const stage = document.querySelector("#xr-stage");
const card = document.createElement("article");
card.className = "tile xr-card";
card.dataset.layoutParticipant = "true";
card.dataset.layoutWidth = "420";
card.dataset.layoutHeight = "132";
card.innerHTML = `<div class="tile-head"><button class="drag-handle xr-collapse" title="Collapse card" aria-label="Collapse card" aria-expanded="true">⠿</button><span class="live-dot"></span><strong class="label">XR</strong><span class="window-actions"><button class="restore xr-size" data-mode="maximize" title="Maximize card" aria-label="Maximize card"><span class="size-glyph"></span></button></span></div><div class="xr-content"><div class="xr-controls"><label><input class="xr-pass" type="checkbox"> Passthrough</label><label><input class="xr-pose-toggle" type="checkbox"> Spatial pose</label><button class="xr-session" disabled>Enter XR</button><button class="xr-lock" type="button" hidden>Lock</button></div><span class="xr-ready">Checking WebXR…</span></div>`;
workspace.append(card);

function ensureHideControl(tile, selected = false) {
  if (tile.classList.contains("xr-card")) {
    tile.querySelector(".xr-lock-hide")?.remove();
    tile.classList.remove("xr-hide-when-locked");
    return;
  }
  const actions = tile.querySelector(".window-actions");
  if (!actions || actions.querySelector(".xr-lock-hide")) return;
  const button = document.createElement("button");
  button.className = "xr-lock-hide";
  button.type = "button";
  button.setAttribute("aria-pressed", String(selected));
  button.addEventListener("click", () => setHideWhenLocked(tile, button.getAttribute("aria-pressed") !== "true"));
  actions.prepend(button);
  setHideWhenLocked(tile, selected);
}
function setHideWhenLocked(tile, selected) {
  const button = tile.querySelector(".xr-lock-hide");
  tile.classList.toggle("xr-hide-when-locked", selected);
  if (!button) return;
  button.setAttribute("aria-pressed", String(selected));
  button.title = selected ? "Show in XR" : "Hide in XR";
  button.setAttribute("aria-label", button.title);
}

new MutationObserver(mutations => {
  const cardsChanged = mutations.some(mutation => mutation.target === workspace &&
    [...mutation.addedNodes, ...mutation.removedNodes].some(node => node.nodeType === 1 && node.matches?.(".tile")));
  if (!card.isConnected) {
    workspace.append(card);
    document.dispatchEvent(new Event("media-layout-request"));
  }
  workspace.querySelectorAll(".tile").forEach(tile => ensureHideControl(tile));
  if (cardsChanged && spatialUi) requestAnimationFrame(() => spatialUi?.syncCards());
}).observe(workspace, { childList: true, subtree: true });
workspace.querySelectorAll(".tile").forEach(tile => ensureHideControl(tile));

const ui = {
  collapse: card.querySelector(".xr-collapse"), size: card.querySelector(".xr-size"),
  pass: card.querySelector(".xr-pass"), poseToggle: card.querySelector(".xr-pose-toggle"),
  enter: card.querySelector(".xr-session"), lock: card.querySelector(".xr-lock"), ready: card.querySelector(".xr-ready"),
};
card.addEventListener("media-card-layout", event => {
  card.dataset.previous = JSON.stringify(event.detail);
  ui.size.dataset.mode = "maximize";
  ui.size.title = "Maximize card";
  ui.size.setAttribute("aria-label", ui.size.title);
});
document.dispatchEvent(new Event("media-layout-request"));
let session = null, space = null, renderer = null, scene = null, camera = null, spatialUi = null, xrOverlay = null;
let socket = null, canVr = false, canAr = false, activeMode = null, locked = false;
let lastPreviewUpdate = 0, firstXrFrame = true;
const poseCards = { left:null, right:null };
const poseValues = {};
let projectionViews = [];
const zero = () => ({ position: { x: 0, y: 0, z: 0 }, orientation: { x: 0, y: 0, z: 0, w: 1 } });
// WebXR is Y-up (+X right, -Z forward). ROS uses fixed Z-up
// (+X forward, +Y left, +Z up), matching the former webxr_reader.
const Q_YUP_TO_ZUP = { x: 0.5, y: -0.5, z: -0.5, w: 0.5 };

function inputOf(source) {
  if (source.hand) return { trigger_pressed:false, grip_pressed:false, stick_clicked:false, face_primary:false, face_secondary:false, trigger_analog:0, grip_analog:0, stick_x:0, stick_y:0 };
  const b = source.gamepad?.buttons || [], a = source.gamepad?.axes || [], n = a.length >= 4 ? 2 : 0;
  return { trigger_pressed: !!b[0]?.pressed, grip_pressed: !!b[1]?.pressed, stick_clicked: !!b[3]?.pressed, face_primary: !!b[4]?.pressed, face_secondary: !!b[5]?.pressed, trigger_analog: Number(b[0]?.value || 0), grip_analog: Number(b[1]?.value || 0), stick_x: Number(a[n] || 0), stick_y: Number(a[n + 1] || 0) };
}
function poseOf(pose) {
  if (!pose) return { ...zero(), tracked: false };
  const p = pose.transform.position, q = pose.transform.orientation;
  return { position: { x: p.x, y: p.y, z: p.z }, orientation: { x: q.x, y: q.y, z: q.z, w: q.w }, tracked: true };
}
function conj(q) { return { x: -q.x, y: -q.y, z: -q.z, w: q.w }; }
function mul(a, b) { return { x:a.w*b.x+a.x*b.w+a.y*b.z-a.z*b.y, y:a.w*b.y-a.x*b.z+a.y*b.w+a.z*b.x, z:a.w*b.z+a.x*b.y-a.y*b.x+a.z*b.w, w:a.w*b.w-a.x*b.x-a.y*b.y-a.z*b.z }; }
function rotate(v, q) { const r = mul(mul(q, { ...v, w: 0 }), conj(q)); return { x:r.x, y:r.y, z:r.z }; }
function yupToZup(pose) {
  if (!pose?.tracked) return pose;
  const p = pose.position, q = pose.orientation;
  const mapped = mul(mul(Q_YUP_TO_ZUP, q), conj(Q_YUP_TO_ZUP));
  return { ...pose,
    position: { x:-p.z, y:-p.x, z:p.y },
    orientation: { x:mapped.x, y:mapped.y, z:mapped.z, w:mapped.w },
  };
}
function remapJoints(joints) {
  return Object.fromEntries(Object.entries(joints || {}).map(([name, pose]) => [name, yupToZup(pose)]));
}
function zUpHand(hand) {
  if (!hand?.tracked) return hand;
  return {
    ...yupToZup(hand),
    input:hand.input,
    source_type:hand.source_type,
    joints:remapJoints(hand.joints),
  };
}
const jointNames = [
  "wrist", "thumb-metacarpal", "thumb-phalanx-proximal", "thumb-phalanx-distal", "thumb-tip",
  "index-finger-metacarpal", "index-finger-phalanx-proximal", "index-finger-phalanx-intermediate", "index-finger-phalanx-distal", "index-finger-tip",
  "middle-finger-metacarpal", "middle-finger-phalanx-proximal", "middle-finger-phalanx-intermediate", "middle-finger-phalanx-distal", "middle-finger-tip",
  "ring-finger-metacarpal", "ring-finger-phalanx-proximal", "ring-finger-phalanx-intermediate", "ring-finger-phalanx-distal", "ring-finger-tip",
  "pinky-finger-metacarpal", "pinky-finger-phalanx-proximal", "pinky-finger-phalanx-intermediate", "pinky-finger-phalanx-distal", "pinky-finger-tip",
];
function jointsOf(source, frame) {
  if (!source.hand) return {};
  const joints = {};
  jointNames.forEach(name => {
    const jointSpace = source.hand.get?.(name);
    if (!jointSpace) return;
    const jointPose = frame.getJointPose
      ? frame.getJointPose(jointSpace, space)
      : frame.getPose(jointSpace, space);
    const pose = poseOf(jointPose);
    if (pose.tracked) joints[name] = { ...pose, radius: Number(jointPose?.radius || 0) };
  });
  return joints;
}
function inputFor(source, joints) {
  if (!source.hand) return inputOf(source);
  const input = inputOf(source);
  const thumb = joints["thumb-tip"]?.position;
  const index = joints["index-finger-tip"]?.position;
  if (!thumb || !index) return input;
  const distance = Math.hypot(thumb.x-index.x, thumb.y-index.y, thumb.z-index.z);
  input.trigger_pressed = distance < 0.025;
  input.trigger_analog = Math.max(0, Math.min(1, 1 - distance / 0.05));
  return input;
}
function createPoseCard(side) {
  if (poseCards[side]?.isConnected) return poseCards[side];
  const poseCard = document.createElement("article");
  poseCards[side] = poseCard;
  poseCard.className = "tile xr-pose-card";
  poseCard.dataset.poseSide = side;
  poseCard.dataset.layoutParticipant = "true";
  poseCard.dataset.layoutWidth = "620";
  poseCard.dataset.layoutHeight = "360";
  poseCard.innerHTML = `<div class="tile-head"><button class="drag-handle collapse-toggle" title="Collapse card" aria-label="Collapse card" aria-expanded="true">⠿</button><span class="live-dot"></span><strong class="label">${side === "left" ? "Left" : "Right"} pose</strong></div><canvas class="xr-pose-canvas" width="900" height="500"></canvas>`;
  poseCard.querySelector(".collapse-toggle").addEventListener("click", event => {
    const collapsed = poseCard.classList.toggle("collapsed");
    event.currentTarget.setAttribute("aria-expanded", String(!collapsed));
  });
  poseCard.addEventListener("pointerdown", event => {
    if (event.button !== 0 || !event.target.closest(".tile-head") || event.target.closest("button,input,label")) return;
    const start = { x:event.clientX, y:event.clientY, left:poseCard.offsetLeft, top:poseCard.offsetTop };
    poseCard.setPointerCapture(event.pointerId);
    const move = current => {
      poseCard.style.left = `${Math.max(0, Math.min(innerWidth-poseCard.offsetWidth, start.left+current.clientX-start.x))}px`;
      poseCard.style.top = `${Math.max(0, Math.min(innerHeight-poseCard.offsetHeight, start.top+current.clientY-start.y))}px`;
    };
    const stop = () => { poseCard.removeEventListener("pointermove", move); poseCard.removeEventListener("pointerup", stop); poseCard.removeEventListener("pointercancel", stop); };
    poseCard.addEventListener("pointermove", move); poseCard.addEventListener("pointerup", stop); poseCard.addEventListener("pointercancel", stop);
  });
  workspace.append(poseCard);
  renderPoseVisualization(side);
  document.dispatchEvent(new Event("media-layout-request"));
  return poseCard;
}
function setPosePreview(enabled) {
  ui.poseToggle.checked = !!enabled;
  Object.keys(poseCards).forEach(side => { poseCards[side]?.remove(); poseCards[side] = null; });
  xrOverlay?.setEnabled(enabled);
}
function posePreviewEnabled() { return ui.poseToggle.checked; }
function showPose(name, pose) {
  if (!posePreviewEnabled()) return;
  poseValues[name] = pose;
  if (name === "hmd") Object.keys(poseCards).forEach(renderPoseVisualization);
  else if (name in poseCards) renderPoseVisualization(name);
}
const handBones = [
  ["wrist","thumb-metacarpal"],["thumb-metacarpal","thumb-phalanx-proximal"],["thumb-phalanx-proximal","thumb-phalanx-distal"],["thumb-phalanx-distal","thumb-tip"],
  ["wrist","index-finger-metacarpal"],["index-finger-metacarpal","index-finger-phalanx-proximal"],["index-finger-phalanx-proximal","index-finger-phalanx-intermediate"],["index-finger-phalanx-intermediate","index-finger-phalanx-distal"],["index-finger-phalanx-distal","index-finger-tip"],
  ["wrist","middle-finger-metacarpal"],["middle-finger-metacarpal","middle-finger-phalanx-proximal"],["middle-finger-phalanx-proximal","middle-finger-phalanx-intermediate"],["middle-finger-phalanx-intermediate","middle-finger-phalanx-distal"],["middle-finger-phalanx-distal","middle-finger-tip"],
  ["wrist","ring-finger-metacarpal"],["ring-finger-metacarpal","ring-finger-phalanx-proximal"],["ring-finger-phalanx-proximal","ring-finger-phalanx-intermediate"],["ring-finger-phalanx-intermediate","ring-finger-phalanx-distal"],["ring-finger-phalanx-distal","ring-finger-tip"],
  ["wrist","pinky-finger-metacarpal"],["pinky-finger-metacarpal","pinky-finger-phalanx-proximal"],["pinky-finger-phalanx-proximal","pinky-finger-phalanx-intermediate"],["pinky-finger-phalanx-intermediate","pinky-finger-phalanx-distal"],["pinky-finger-phalanx-distal","pinky-finger-tip"],
];
function renderPoseVisualization(side) {
  const poseCard = poseCards[side];
  if (!poseCard?.isConnected) return;
  const canvas = poseCard.querySelector(".xr-pose-canvas");
  const context = canvas.getContext("2d");
  context.fillStyle = "#071013"; context.fillRect(0, 0, canvas.width, canvas.height);
  const center = { x:canvas.width / 2, y:canvas.height * 0.5 };
  const transform = (matrix, point) => ({
    x:matrix[0]*point.x+matrix[4]*point.y+matrix[8]*point.z+matrix[12],
    y:matrix[1]*point.x+matrix[5]*point.y+matrix[9]*point.z+matrix[13],
    z:matrix[2]*point.x+matrix[6]*point.y+matrix[10]*point.z+matrix[14],
    w:matrix[3]*point.x+matrix[7]*point.y+matrix[11]*point.z+matrix[15],
  });
  const project = point => {
    const projected = projectionViews.map(view => {
      const eye=transform(view.view,point), clip=transform(view.projection,eye);
      if (clip.w <= 0.0001) return null;
      const x=clip.x/clip.w, y=clip.y/clip.w;
      if (!Number.isFinite(x)||!Number.isFinite(y)) return null;
      const canvasAspect=canvas.width/canvas.height, aspect=view.aspect || canvasAspect;
      const width=aspect>canvasAspect ? canvas.width : canvas.height*aspect;
      const height=aspect>canvasAspect ? canvas.width/aspect : canvas.height;
      return { x:(canvas.width-width)/2+(x*.5+.5)*width, y:(canvas.height-height)/2+(.5-y*.5)*height };
    }).filter(Boolean);
    if (!projected.length) return null;
    return { x:projected.reduce((sum,p)=>sum+p.x,0)/projected.length, y:projected.reduce((sum,p)=>sum+p.y,0)/projected.length };
  };
  const line = (from, to, color, width=3) => {
    const a=project(from), b=project(to); if (!a || !b) return;
    context.strokeStyle=color; context.lineWidth=width;
    context.beginPath(); context.moveTo(a.x,a.y); context.lineTo(b.x,b.y); context.stroke();
  };
  const axes = (position, orientation, length=0.16) => {
    const basis = [[{x:length,y:0,z:0},"#ff5964","X"],[{x:0,y:length,z:0},"#54e38e","Y"],[{x:0,y:0,z:length},"#57a5ff","Z"]];
    basis.forEach(([axis,color,label]) => {
      const direction=rotate(axis, orientation || {x:0,y:0,z:0,w:1});
      const end={x:position.x+direction.x,y:position.y+direction.y,z:position.z+direction.z};
      line(position,end,color,4); const p=project(end); if (!p) return;
      context.fillStyle=color; context.font="bold 20px sans-serif"; context.fillText(label,p.x+5,p.y-4);
    });
  };
  const hmd=poseValues.hmd;
  if (hmd?.tracked && projectionViews.length) {
    const originOffset=rotate({x:0,y:0,z:-1},hmd.orientation);
    const origin={x:hmd.position.x+originOffset.x,y:hmd.position.y+originOffset.y,z:hmd.position.z+originOffset.z};
    axes(origin,hmd.orientation,0.18);
    context.strokeStyle="#c8d9d5"; context.lineWidth=2;
    context.beginPath(); context.arc(center.x,center.y,12,0,Math.PI*2); context.stroke();
  }
  [side].forEach(key => {
    const hand=poseValues[key]; if (!hand?.tracked) return;
    const color=key === "right" ? "#f4a261" : "#58d6bd";
    if (hand.source_type === "hand" && Object.keys(hand.joints || {}).length) {
      handBones.forEach(([a,b]) => { const pa=hand.joints[a]?.position, pb=hand.joints[b]?.position; if(pa&&pb) line(pa,pb,color,4); });
      Object.values(hand.joints).forEach(joint => { const p=project(joint.position); if(!p)return; context.fillStyle=color; context.beginPath(); context.arc(p.x,p.y,5,0,Math.PI*2); context.fill(); });
    } else {
      const spatial=hand;
      axes(spatial.position,spatial.orientation,0.11);
      const p=project(spatial.position); if(!p)return; context.fillStyle=color; context.beginPath(); context.arc(p.x,p.y,10,0,Math.PI*2); context.fill();
    }
    const spatial=hand;
    const label=project(spatial.position); if(!label)return; context.fillStyle=color; context.font="bold 19px sans-serif"; context.fillText(key.toUpperCase(),label.x+12,label.y-10);
  });
  if (!poseValues[side]?.tracked) {
    context.fillStyle="#687d78"; context.font="22px sans-serif"; context.textAlign="center";
    context.fillText("Waiting for controller or hand tracking",center.x,canvas.height-34); context.textAlign="start";
  }
}
function openSocket() {
  const connection = new WebSocket(`${location.protocol === "https:" ? "wss" : "ws"}://${location.host}/xr-signal`);
  socket = connection;
  return new Promise((resolve, reject) => {
    let admitted = false;
    let denied = false;
    connection.addEventListener("message", (event) => {
      const message = JSON.parse(event.data);
      if (message.op === "session.config") {
        admitted = true;
        resolve();
      } else if (message.op === "session.busy") {
        denied = true;
        reject(new Error(message.message));
      }
    });
    connection.addEventListener("error", () => reject(new Error("XR connection failed")));
    connection.addEventListener("close", () => {
      if (!admitted) reject(new Error("XR connection closed"));
      if (admitted && !denied && session && socket === connection) {
        setTimeout(() => {
          if (!session || socket !== connection) return;
          openSocket().catch(async (error) => {
            await session?.end().catch(() => {});
            ui.ready.textContent = error.message;
          });
        }, 1200);
      }
    });
  });
}
function reportError(phase, error) {
  const body = JSON.stringify({ phase, message:error?.message || String(error), stack:error?.stack || "" });
  fetch("/api/xr/error", { method:"POST", headers:{ "content-type":"application/json" }, body, keepalive:true }).catch(() => {});
  console.error(`XR ${phase} error`, error);
}
function reportPhase(phase, details = {}) {
  fetch("/api/xr/event", { method:"POST", headers:{ "content-type":"application/json" }, body:JSON.stringify({ phase, details }), keepalive:true }).catch(() => {});
}
function onFrame(_time, frame) {
  if (!session) return;
  try {
    renderFrame(_time, frame);
  } catch (error) {
    ui.ready.textContent = "XR render error";
    if (socket?.readyState === WebSocket.OPEN) socket.send(JSON.stringify({ op:"client.error", message:error.message, stack:error.stack || "" }));
    reportError("render", error);
    if (spatialUi?.nativeQuad) {
      const failedUi = spatialUi;
      try {
        session.updateRenderState({ layers:[renderer.xr.getBaseLayer()] });
        spatialUi = new SpatialCardUI(
          scene, camera, session, space,
          { lock:() => setLocked(true), exit:() => session?.end() },
        );
        failedUi.dispose();
        reportPhase("quad-fallback", { panels:spatialUi.panels.length });
        ui.ready.textContent = "XR active";
      } catch (fallbackError) {
        reportError("quad-fallback", fallbackError);
      }
    }
    try {
      renderer.render(scene, camera);
    } catch (renderError) {
      reportError("projection-render", renderError);
    }
  }
}
function renderFrame(_time, frame) {
  const viewer = frame.getViewerPose(space);
  if (!viewer) return;
  const hmd = poseOf({ transform: viewer.transform });
  const hands = {
    left: { ...zero(), input: inputOf({}), tracked: false, source_type:"none", joints:{} },
    right: { ...zero(), input: inputOf({}), tracked: false, source_type:"none", joints:{} },
  };
  for (const source of session.inputSources) {
    if (!(source.handedness in hands)) continue;
    const sourceType = source.hand ? "hand" : "controller";
    if (hands[source.handedness].source_type === "hand" && sourceType !== "hand") continue;
    const wrist = source.hand?.get?.("wrist");
    const sourceSpace = wrist || source.gripSpace || source.targetRaySpace;
    const sourcePose = wrist && frame.getJointPose
      ? frame.getJointPose(wrist, space)
      : sourceSpace ? frame.getPose(sourceSpace, space) : null;
    const joints = jointsOf(source, frame);
    const spatialPose = poseOf(sourcePose);
    hands[source.handedness] = {
      ...spatialPose,
      input: inputFor(source, joints), source_type:sourceType, joints,
    };
  }
  const zUpHmd = yupToZup(hmd);
  const zUpHands = { left:zUpHand(hands.left), right:zUpHand(hands.right) };
  xrOverlay?.update(zUpHands);
  if (socket?.readyState === WebSocket.OPEN) {
    socket.send(JSON.stringify({ op:"xr.sample", t_ms:Date.now(), headset_id:"headset", xr_mode:activeMode, passthrough:activeMode === "immersive-ar", preview:posePreviewEnabled(), ui_locked:locked, hmd:zUpHmd, hmd_tracked:true, left:zUpHands.left, right:zUpHands.right }));
  }
  spatialUi?.setFrame?.(frame);
  spatialUi?.update(frame, viewer, _time);
  renderer.render(scene, camera);
  if (firstXrFrame) {
    firstXrFrame = false;
    const glError = renderer.getContext().getError();
    reportPhase("first-frame", {
      views:viewer.views.length,
      panels:spatialUi?.panels?.length || spatialUi?.cards?.length || 0,
      spatial:spatialUi?.debugState() || null,
      drawCalls:renderer.info.render.calls,
      triangles:renderer.info.render.triangles,
      glError,
    });
  }
}
function requestedMode() {
  return ui.pass.checked ? "immersive-ar" : "immersive-vr";
}
function updateReadiness() {
  const ready = ui.pass.checked ? canAr : canVr;
  ui.enter.disabled = !ready || !!session;
  ui.ready.textContent = ready ? "Ready" : ui.pass.checked ? "Passthrough unavailable" : "VR unavailable";
}
async function enter() {
  const mode = requestedMode();
  if (mode === "immersive-ar" ? !canAr : !canVr) throw new Error(`${mode} unavailable`);
  session = await navigator.xr.requestSession(mode, { requiredFeatures:["local-floor"], optionalFeatures:["dom-overlay","hand-tracking"], domOverlay:{ root:document.body } });
  reportPhase("session-created", { mode, overlay:!!session.domOverlayState });
  try {
    await openSocket();
    ({ renderer, scene, camera } = createXrRenderer(stage, mode === "immersive-vr"));
    xrOverlay = new SpatialXrOverlay(scene);
    // Overlay data uses ROS Z-up coordinates. Rotate that coordinate space
    // back into WebXR only for rendering, so the labelled X/Y/Z gizmos show
    // the same axes that ROS and RViz receive.
    const renderFromRos = conj(Q_YUP_TO_ZUP);
    xrOverlay.root.quaternion.set(
      renderFromRos.x, renderFromRos.y, renderFromRos.z, renderFromRos.w
    );
    xrOverlay.setEnabled(ui.poseToggle.checked);
    renderer.xr.setReferenceSpaceType("local-floor");
    await renderer.xr.setSession(session);
    space = renderer.xr.getReferenceSpace();
    if (!space) {
      space = await session.requestReferenceSpace("local-floor");
    }
    reportPhase("renderer-ready", { referenceSpace:space.type || "unknown", layer:renderer.xr.getBaseLayer()?.constructor?.name || "unknown" });
  } catch (error) {
    reportError("initialization", error);
    await session.end().catch(() => {});
    session = null;
    socket?.close(); socket=null;
    xrOverlay?.dispose(); xrOverlay=null; renderer?.dispose(); renderer=null; scene=null; camera=null;
    throw new Error(`XR initialization failed: ${error.message}`);
  }
  activeMode = mode; setLocked(false);
  document.body.classList.add("xr-active"); card.classList.add("playing");
  const useSpatialUi = mode === "immersive-vr" || !session.domOverlayState;
  if (useSpatialUi) {
    const callbacks = { lock:() => setLocked(true), exit:() => session?.end() };
    spatialUi = new SpatialCardUI(scene, camera, session, space, callbacks);
    reportPhase("spatial-ui-created", { panels:spatialUi.panels?.length || spatialUi.cards?.length || 0, nativeQuad:!!spatialUi.nativeQuad });
  }
  firstXrFrame = true;
  ui.enter.textContent = "Exit XR"; ui.enter.disabled = false; ui.lock.hidden = false; ui.pass.disabled = true;
  ui.ready.textContent = "XR active";
  session.addEventListener("end", () => { setLocked(false); renderer?.setAnimationLoop(null); spatialUi?.dispose(); spatialUi=null; xrOverlay?.dispose(); xrOverlay=null; renderer?.dispose(); renderer=null; scene=null; camera=null; session=null; space=null; activeMode=null; socket?.close(); socket=null; document.body.classList.remove("xr-active"); card.classList.remove("playing"); ui.enter.textContent="Enter XR"; ui.lock.hidden=true; ui.pass.disabled=false; updateReadiness(); }, { once:true });
  renderer.setAnimationLoop(onFrame);
}

function setLocked(value) {
  locked = !!value;
  document.body.classList.toggle("xr-locked", locked);
  spatialUi?.setLocked(locked);
  ui.lock.hidden = !session || locked;
}

ui.enter.addEventListener("click", () => (session ? session.end() : enter()).catch(error => { ui.ready.textContent = error.message; reportError("enter", error); }));
ui.lock.addEventListener("click", () => setLocked(true));
ui.poseToggle.addEventListener("change", () => setPosePreview(ui.poseToggle.checked));
ui.pass.addEventListener("change", updateReadiness);
ui.collapse.addEventListener("click", () => { const value=card.classList.toggle("collapsed"); ui.collapse.setAttribute("aria-expanded", String(!value)); ui.collapse.title=value ? "Expand card" : "Collapse card"; });
ui.size.addEventListener("click", () => {
  const maximize = ui.size.dataset.mode === "maximize";
  if (maximize) card.dataset.previous = JSON.stringify({ left:card.offsetLeft, top:card.offsetTop, width:card.offsetWidth, height:card.offsetHeight });
  const p = !maximize && card.dataset.previous ? JSON.parse(card.dataset.previous) : { left:12, top:12, width:420, height:220 };
  Object.assign(card.style, maximize ? { left:"0px", top:"0px", width:"100%", height:"100%" } : { left:`${p.left}px`, top:`${p.top}px`, width:`${p.width}px`, height:`${p.height}px` });
  ui.size.dataset.mode = maximize ? "restore" : "maximize";
});
card.addEventListener("pointerdown", event => {
  if (event.button !== 0 || !event.target.closest(".tile-head") || event.target.closest("button,select,input,label")) return;
  const start={ x:event.clientX, y:event.clientY, left:card.offsetLeft, top:card.offsetTop }; card.setPointerCapture(event.pointerId);
  const move=current => { card.style.left=`${Math.max(0,Math.min(innerWidth-card.offsetWidth,start.left+current.clientX-start.x))}px`; card.style.top=`${Math.max(0,Math.min(innerHeight-card.offsetHeight,start.top+current.clientY-start.y))}px`; };
  const stop=() => { card.removeEventListener("pointermove",move); card.removeEventListener("pointerup",stop); card.removeEventListener("pointercancel",stop); }; card.addEventListener("pointermove",move); card.addEventListener("pointerup",stop); card.addEventListener("pointercancel",stop);
});

(async () => {
  if (!navigator.xr) { ui.ready.textContent="WebXR unavailable"; return; }
  canVr=await navigator.xr.isSessionSupported("immersive-vr");
  try { canAr=await navigator.xr.isSessionSupported("immersive-ar"); } catch (_) { canAr=false; }
  updateReadiness();
})().catch(error => { ui.ready.textContent=error.message; });
