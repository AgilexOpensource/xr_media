import * as THREE from "/xr/vendor/three.module.min.js?v=0.180.0-2";

const PANEL_WIDTH = 0.68;
const NATIVE_HEADER_PX = 58;

function roundedRect(context, x, y, width, height, radius) {
  context.beginPath();
  context.roundRect(x, y, width, height, radius);
  context.fill();
}

function cardSource(card) {
  if (card.classList.contains("video-input")) {
    const video = card.querySelector("video.input-preview");
    return video?.readyState >= 2 ? video : null;
  }
  if (card.classList.contains("audio-input")) return card.querySelector("canvas.input-waveform");
  const canvas = card.querySelector("canvas:not(.filter-defs)");
  if (canvas?.width && canvas?.height) return canvas;
  const video = card.querySelector("video");
  return video?.readyState >= 2 ? video : null;
}

function videoSource(card) {
  if (card.classList.contains("audio-tile") || card.classList.contains("audio-input") || card.classList.contains("xr-card")) return null;
  return card.querySelector("video");
}

function visibleInfo(card) {
  return [...card.querySelectorAll(".video-info [data-info], .video-info [data-audio-info]")]
    .filter(node => !node.hidden && node.textContent.trim())
    .map(node => node.textContent.trim());
}

class SpatialCard {
  constructor(card, index) {
    this.card = card;
    this.canvas = document.createElement("canvas");
    this.canvas.width = 512;
    this.canvas.height = 320;
    this.context = this.canvas.getContext("2d");
    this.texture = new THREE.CanvasTexture(this.canvas);
    this.texture.colorSpace = THREE.SRGBColorSpace;
    const ratio = Math.max(1.2, card.offsetWidth / Math.max(card.offsetHeight, 1));
    this.width = PANEL_WIDTH;
    this.height = PANEL_WIDTH / ratio;
    this.mesh = new THREE.Mesh(
      new THREE.PlaneGeometry(this.width, this.height),
      new THREE.MeshBasicMaterial({ map: this.texture, transparent: false, side: THREE.DoubleSide, depthTest: false, depthWrite: false }),
    );
    this.backplate = new THREE.Mesh(
      new THREE.PlaneGeometry(this.width + 0.018, this.height + 0.018),
      new THREE.MeshBasicMaterial({ color: 0x58d6bd, side: THREE.DoubleSide, depthTest: false, depthWrite: false }),
    );
    this.backplate.position.z = -0.004;
    this.backplate.renderOrder = 999;
    this.mesh.add(this.backplate);
    this.mesh.frustumCulled = false;
    this.mesh.userData.panel = this;
    this.video = videoSource(card);
    this.controlHits = [];
    this.locked = false;
    this.lastDraw = 0;
    this.screenRect = card.getBoundingClientRect();
    this.mesh.position.set(0, 0, -0.95);
    this.backplate.visible = false;
    this.setStack(index + 1);
    this.draw(0, true);
  }

  setStack(stack) {
    this.stack = stack;
    this.backplate.renderOrder = 1000 + stack * 2;
    this.mesh.renderOrder = 1001 + stack * 2;
  }

  draw(time = 0, force = false) {
    const audio = this.card.classList.contains("audio-tile") || this.card.classList.contains("audio-input");
    if (!force && time - this.lastDraw < (this.video ? 66 : audio ? 33 : 125)) return;
    const context = this.context;
    context.fillStyle = "#05090b";
    context.fillRect(0, 0, this.canvas.width, this.canvas.height);
    const contentTop = 0;
    const source = this.video?.readyState >= 2 ? this.video : cardSource(this.card);
    if (source) {
      try {
        if (source instanceof HTMLCanvasElement) source.xrRefresh?.();
        context.drawImage(source, 0, contentTop, this.canvas.width, this.canvas.height - contentTop);
      } catch (_) {}
    } else {
      context.fillStyle = "#829995";
      context.font = "18px sans-serif";
      context.fillText("Waiting for media", 28, contentTop + 42);
    }
    this.controlHits = [];
    if (!this.locked) {
      if (this.card.classList.contains("xr-card")) this.drawXrControls();
      else this.drawDomControls();
    }
    if (!this.card.classList.contains("xr-card")) this.drawInfo();
    this.texture.needsUpdate = true;
    this.lastDraw = time;
  }

  drawXrControls() {
    const context = this.context;
    context.fillStyle = "#102127";
    context.fillRect(0, 0, this.canvas.width, this.canvas.height);
    this.button(context, 30, 112, 220, 72, "Lock");
    this.button(context, 270, 112, 220, 72, "Exit XR");
  }

  button(context, x, y, width, height, label) {
    context.fillStyle = "#24434c";
    roundedRect(context, x, y, width, height, 10);
    context.fillStyle = "#f2fbf9";
    context.font = "600 22px sans-serif";
    context.fillText(label, x + 22, y + height / 2);
  }

  drawDomControls() {
    const controls = [...this.card.querySelectorAll("button, select, summary")].filter(control =>
      !control.matches(".drag-handle, .restore") && !control.disabled && !control.hidden && !control.closest("[hidden]"),
    ).slice(0, 7);
    this.controlHits = [];
    if (!controls.length) return;
    const gap = 6;
    const width = (this.canvas.width - 20 - gap * (controls.length - 1)) / controls.length;
    const y = this.canvas.height - 48;
    this.context.fillStyle = "rgba(5, 9, 11, .82)";
    this.context.fillRect(0, y - 8, this.canvas.width, 56);
    controls.forEach((control, index) => {
      const x = 10 + index * (width + gap);
      const label = control.tagName === "SUMMARY" ? "Info" : control.tagName === "SELECT"
        ? control.options[control.selectedIndex]?.text || control.value
        : control.getAttribute("aria-label") || control.title || control.textContent.trim() || "Control";
      this.context.fillStyle = "#24434c";
      roundedRect(this.context, x, y, width, 38, 7);
      this.context.fillStyle = "#f2fbf9";
      this.context.font = "600 15px sans-serif";
      this.context.textAlign = "center";
      this.context.fillText(label.slice(0, 10), x + width / 2, y + 19, width - 8);
      this.context.textAlign = "start";
      this.controlHits.push({ control, x, y, width, height:38 });
    });
  }

  drawInfo() {
    const values = visibleInfo(this.card);
    if (!values.length) return;
    const text = values.join("  ·  ");
    const y = this.locked ? this.canvas.height - 34 : this.canvas.height - 88;
    this.context.fillStyle = "rgba(2, 7, 8, .68)";
    this.context.fillRect(0, y, this.canvas.width, 34);
    this.context.fillStyle = "#edf7f5";
    this.context.font = "15px ui-monospace, monospace";
    this.context.textBaseline = "middle";
    this.context.fillText(text, 12, y + 17, this.canvas.width - 24);
  }

  hitAction(uv) {
    const x = uv.x * this.canvas.width;
    const y = (1 - uv.y) * this.canvas.height;
    const target = this.controlHits.find(item =>
      x >= item.x && x <= item.x + item.width && y >= item.y && y <= item.y + item.height,
    );
    if (target) {
      const control = target.control;
      if (control.tagName === "SELECT" && control.options.length) {
        control.selectedIndex = (control.selectedIndex + 1) % control.options.length;
        control.dispatchEvent(new Event("change", { bubbles:true }));
      } else if (control.tagName === "SUMMARY") {
        this.cycleInfo();
      } else {
        control.click();
      }
      this.draw(performance.now(), true);
      return "control";
    }
    if (!this.card.classList.contains("xr-card")) return null;
    if (y >= 112 && y <= 184 && x >= 30 && x <= 250) return "lock";
    if (y >= 112 && y <= 184 && x >= 270 && x <= 490) return "exit";
    return null;
  }

  cycleInfo() {
    const options = [...this.card.querySelectorAll(".info-options input[type=checkbox]")];
    if (!options.length) return;
    const checked = options.filter(option => option.checked);
    if (checked.length === options.length || (checked.length > 1 && checked.length < options.length)) {
      options.forEach((option, index) => { option.checked = index === 0; });
    } else if (checked.length === 1) {
      const next = options.indexOf(checked[0]) + 1;
      options.forEach((option, index) => { option.checked = next < options.length && index === next; });
    } else {
      options.forEach(option => { option.checked = true; });
    }
    options[0].dispatchEvent(new Event("change", { bubbles:true }));
  }

  dispose() {
    this.mesh.geometry.dispose();
    this.mesh.material.dispose();
    this.backplate.geometry.dispose();
    this.backplate.material.dispose();
    this.texture.dispose();
  }

  setLocked(locked) {
    this.locked = locked;
    this.backplate.visible = false;
    this.draw(performance.now(), true);
  }
}

export class SpatialCardUI {
  constructor(scene, camera, session, referenceSpace, callbacks) {
    this.scene = scene;
    this.camera = camera;
    this.session = session;
    this.referenceSpace = referenceSpace;
    this.callbacks = callbacks;
    this.root = new THREE.Group();
    this.scene.add(this.root);
    this.raycaster = new THREE.Raycaster();
    this.panels = [];
    this.lines = new Map();
    this.reticles = new Map();
    this.hits = new Map();
    this.triggerPressed = new Map();
    this.drag = null;
    this.lastDraw = 0;
    this.locked = false;
    this.topStack = 0;
    this.syncCards();
    this.onSelectStart = event => {
      this.triggerPressed.set(event.inputSource, true);
      if (this.locked) return;
      this.selectStart(event.inputSource);
    };
    this.onSelectEnd = event => {
      this.triggerPressed.set(event.inputSource, false);
      if (this.drag?.source === event.inputSource) this.drag = null;
    };
    this.onInputSourcesChange = event => {
      for (const source of event.removed || []) this.removeInputSource(source);
    };
    session.addEventListener("selectstart", this.onSelectStart);
    session.addEventListener("selectend", this.onSelectEnd);
    session.addEventListener("inputsourceschange", this.onInputSourcesChange);
  }

  removeInputSource(source) {
    const line=this.lines.get(source);
    if(line) { this.scene.remove(line); line.geometry.dispose(); line.material.dispose(); this.lines.delete(source); }
    const reticle=this.reticles.get(source);
    if(reticle) { this.scene.remove(reticle); reticle.geometry.dispose(); reticle.material.dispose(); this.reticles.delete(source); }
    this.hits.delete(source); this.triggerPressed.delete(source);
    if(this.drag?.source===source) this.drag=null;
  }

  syncInputSources() {
    const active=new Set(this.session.inputSources);
    const known=new Set([...this.lines.keys(),...this.reticles.keys(),...this.hits.keys(),...this.triggerPressed.keys()]);
    known.forEach(source => { if(!active.has(source)) this.removeInputSource(source); });
  }

  syncCards() {
    this.panels.forEach(panel => { this.root.remove(panel.mesh); panel.dispose(); });
    const cards = [...document.querySelectorAll("#workspace > .tile")].filter(card =>
      card.classList.contains("xr-card") || !card.classList.contains("xr-hide-when-locked"),
    );
    this.panels = cards.map((card, index) => new SpatialCard(card, index));
    this.panels.forEach(panel => this.root.add(panel.mesh));
    this.layoutPanels();
    this.applyLockedVisibility();
    return this.panels.length;
  }

  layoutPanels() {
    const xrPanel = this.panels.find(panel => panel.card.classList.contains("xr-card"));
    const media = this.panels.filter(panel => panel !== xrPanel);
    const rows = [];
    [...media]
      .sort((left, right) => left.screenRect.top - right.screenRect.top || left.screenRect.left - right.screenRect.left)
      .forEach(panel => {
        const center = panel.screenRect.top + panel.screenRect.height / 2;
        let row = rows.find(candidate =>
          Math.abs(center - candidate.center) <= Math.max(32, Math.min(panel.screenRect.height, candidate.height) * 0.5),
        );
        if (!row) {
          row = { center, height:panel.screenRect.height, panels:[] };
          rows.push(row);
        }
        row.panels.push(panel);
        row.center = row.panels.reduce((sum, item) => sum + item.screenRect.top + item.screenRect.height / 2, 0) / row.panels.length;
        row.height = Math.max(row.height, panel.screenRect.height);
      });
    rows.sort((left, right) => left.center - right.center);
    rows.forEach(row => row.panels.sort((left, right) => left.screenRect.left - right.screenRect.left));
    const widest = Math.max(0, ...rows.map(row => row.panels.reduce((sum, panel) => sum + panel.width, 0)));
    const totalHeight = rows.reduce((sum, row) => sum + Math.max(...row.panels.map(panel => panel.height)), 0);
    const scale = Math.min(1, widest ? 1.6 / widest : 1, totalHeight ? 1.05 / totalHeight : 1);
    let yCursor = -totalHeight * scale / 2;
    rows.forEach(row => {
      const rowHeight = Math.max(...row.panels.map(panel => panel.height)) * scale;
      const rowWidth = row.panels.reduce((sum, panel) => sum + panel.width * scale, 0);
      let xCursor = -rowWidth / 2;
      row.panels.forEach(panel => {
        const width = panel.width * scale;
        panel.mesh.scale.setScalar(scale);
        panel.mesh.position.set(xCursor + width / 2, -(yCursor + rowHeight / 2), -0.95);
        xCursor += width;
      });
      yCursor += rowHeight;
    });
    this.topStack = media.length;
    media.forEach((panel, index) => panel.setStack(index + 1));
    if (xrPanel) {
      xrPanel.mesh.scale.setScalar(0.85);
      xrPanel.mesh.position.set(0, 0, -0.72);
      xrPanel.setStack(this.topStack + 1000);
    }
    this.panels.forEach(panel => this.orientPanel(panel));
  }

  orientPanel(panel) {
    if (panel.card.classList.contains("xr-card")) {
      panel.mesh.rotation.set(0, 0, 0);
      return;
    }
    const depth = Math.max(0.35, -panel.mesh.position.z);
    const yaw = Math.atan2(-panel.mesh.position.x, depth);
    const pitch = Math.atan2(panel.mesh.position.y, depth);
    panel.mesh.rotation.set(
      THREE.MathUtils.clamp(
        pitch, THREE.MathUtils.degToRad(-24), THREE.MathUtils.degToRad(24),
      ),
      THREE.MathUtils.clamp(
        yaw, THREE.MathUtils.degToRad(-32), THREE.MathUtils.degToRad(32),
      ),
      0,
    );
  }

  rayFor(source, frame) {
    const pose = frame.getPose(source.targetRaySpace, this.referenceSpace);
    if (!pose) return null;
    const position = pose.transform.position;
    const orientation = pose.transform.orientation;
    const origin = new THREE.Vector3(position.x, position.y, position.z);
    const quaternion = new THREE.Quaternion(orientation.x, orientation.y, orientation.z, orientation.w);
    const direction = new THREE.Vector3(0, 0, -1).applyQuaternion(quaternion).normalize();
    return { origin, direction };
  }

  update(frame, viewer, time) {
    this.syncInputSources();
    const position = viewer.transform.position;
    const orientation = viewer.transform.orientation;
    this.root.position.set(position.x, position.y, position.z);
    this.root.quaternion.set(orientation.x, orientation.y, orientation.z, orientation.w);
    this.root.updateMatrixWorld(true);
    this.panels.forEach(panel => panel.draw(time));
    if (this.locked) {
      this.drag = null;
      this.hits.clear();
      this.reticles.forEach(reticle => { reticle.visible = false; });
      for (const source of this.session.inputSources) {
        if (source.gamepad) this.triggerPressed.set(source, !!source.gamepad.buttons?.[0]?.pressed);
      }
      return;
    }
    for (const source of this.session.inputSources) {
      if (!source.targetRaySpace) continue;
      const ray = this.rayFor(source, frame);
      if (!ray) {
        const line=this.lines.get(source), reticle=this.reticles.get(source);
        if(line) line.visible=false; if(reticle) reticle.visible=false;
        this.hits.delete(source);
        continue;
      }
      const hit = this.intersections(ray)[0] || null;
      this.hits.set(source, hit);
      this.updateLine(source, ray, hit);
      if (source.gamepad) {
        const pressed = !!source.gamepad.buttons?.[0]?.pressed;
        const wasPressed = this.triggerPressed.get(source) || false;
        if (pressed && !wasPressed) this.selectStart(source);
        if (!pressed && wasPressed && this.drag?.source === source) this.drag = null;
        this.triggerPressed.set(source, pressed);
      }
      if (this.drag?.source === source) this.updateDrag(ray, source, time);
    }
  }

  updateLine(source, ray, hit) {
    let line = this.lines.get(source);
    if (!line) {
      line = new THREE.Line(
        new THREE.BufferGeometry().setFromPoints([new THREE.Vector3(), new THREE.Vector3(0, 0, -1)]),
        new THREE.LineBasicMaterial({ color: 0x58d6bd, depthTest:false, depthWrite:false }),
      );
      line.renderOrder = 1000000;
      this.scene.add(line); this.lines.set(source, line);
    }
    line.position.copy(ray.origin);
    line.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, -1), ray.direction);
    line.scale.z = hit ? hit.distance : 2;
    line.visible = true;
    let reticle = this.reticles.get(source);
    if (!reticle) {
      reticle = new THREE.Mesh(
        new THREE.RingGeometry(0.008, 0.014, 24),
        new THREE.MeshBasicMaterial({ color: 0x7ae4cf, side: THREE.DoubleSide, depthTest: false }),
      );
      reticle.renderOrder = 1000001;
      this.scene.add(reticle); this.reticles.set(source, reticle);
    }
    reticle.visible = !!hit;
    if (hit) {
      reticle.position.copy(hit.point).addScaledVector(ray.direction, -0.003);
      reticle.quaternion.copy(hit.object.getWorldQuaternion(new THREE.Quaternion()));
    }
  }

  intersections(ray) {
    this.raycaster.set(ray.origin, ray.direction);
    return this.raycaster.intersectObjects(
      this.panels.filter(panel => panel.mesh.visible).map(panel => panel.mesh), false,
    ).sort((left, right) => right.object.userData.panel.stack - left.object.userData.panel.stack);
  }

  selectStart(source) {
    const hit = this.hits.get(source);
    if (!hit) return;
    const panel = hit.object.userData.panel;
    this.bringToFront(panel);
    const action = panel.hitAction(hit.uv);
    if (action === "lock") this.callbacks.lock();
    else if (action === "exit") this.callbacks.exit();
    else if (action !== "control") this.beginDrag(source, panel, hit.point);
  }

  bringToFront(panel) {
    const xrPanel = this.panels.find(item => item.card.classList.contains("xr-card"));
    if (panel !== xrPanel) panel.setStack(++this.topStack);
    if (xrPanel) xrPanel.setStack(this.topStack + 1000);
  }

  beginDrag(source, panel, worldPoint) {
    const point = worldPoint.clone();
    this.root.worldToLocal(point);
    this.drag = { source, panel, offset:panel.mesh.position.clone().sub(point) };
  }

  updateDrag(ray, source, time) {
    const panel = this.drag.panel;
    const normal = new THREE.Vector3(0, 0, 1).applyQuaternion(panel.mesh.getWorldQuaternion(new THREE.Quaternion()));
    const plane = new THREE.Plane().setFromNormalAndCoplanarPoint(normal, panel.mesh.getWorldPosition(new THREE.Vector3()));
    const point = new THREE.Ray(ray.origin, ray.direction).intersectPlane(plane, new THREE.Vector3());
    if (!point) return;
    this.root.worldToLocal(point);
    panel.mesh.position.x = THREE.MathUtils.clamp(point.x + this.drag.offset.x, -1.1, 1.1);
    panel.mesh.position.y = THREE.MathUtils.clamp(point.y + this.drag.offset.y, -0.7, 0.8);
    const axes = source.gamepad?.axes || [];
    const stickY = Number(axes[axes.length >= 4 ? 3 : 1] || 0);
    const elapsed = Math.min(50, Math.max(0, time - (this.drag.lastTime || time)));
    if (Math.abs(stickY) > 0.15) {
      const requestedDelta = stickY * elapsed * 0.0012;
      if (panel.card.classList.contains("xr-card")) {
        const minDelta = Math.max(...this.panels.map(item => -2.5 - item.mesh.position.z));
        const maxDelta = Math.min(...this.panels.map(item => -0.35 - item.mesh.position.z));
        const delta = THREE.MathUtils.clamp(requestedDelta, minDelta, maxDelta);
        this.panels.forEach(item => { item.mesh.position.z += delta; });
      } else {
        panel.mesh.position.z = THREE.MathUtils.clamp(
          panel.mesh.position.z + requestedDelta, -2.5, -0.35,
        );
      }
    }
    if (panel.card.classList.contains("xr-card")) {
      this.panels.forEach(item => this.orientPanel(item));
    } else {
      this.orientPanel(panel);
    }
    this.drag.lastTime = time;
  }

  setFrame(frame) { this.currentFrame = frame; }

  debugState() {
    const panel = this.panels[0]?.mesh;
    const panelPosition = panel?.getWorldPosition(new THREE.Vector3());
    return {
      root: this.root.position.toArray().map(value => Number(value.toFixed(3))),
      panel: panelPosition?.toArray().map(value => Number(value.toFixed(3))) || null,
    };
  }

  setLocked(locked) {
    this.locked = locked;
    this.drag = null;
    this.hits.clear();
    this.applyLockedVisibility();
    this.lines.forEach(line => { line.visible = !locked; });
    this.reticles.forEach(reticle => { reticle.visible = false; });
  }

  applyLockedVisibility() {
    this.root.visible = true;
    this.panels.forEach(panel => {
      panel.mesh.visible = !this.locked || !panel.card.classList.contains("xr-card");
      panel.setLocked(this.locked);
    });
  }

  dispose() {
    this.session.removeEventListener("selectstart", this.onSelectStart);
    this.session.removeEventListener("selectend", this.onSelectEnd);
    this.session.removeEventListener("inputsourceschange", this.onInputSourcesChange);
    this.panels.forEach(panel => panel.dispose());
    this.lines.forEach(line => { this.scene.remove(line); line.geometry.dispose(); line.material.dispose(); });
    this.reticles.forEach(reticle => { this.scene.remove(reticle); reticle.geometry.dispose(); reticle.material.dispose(); });
    this.triggerPressed.clear();
    this.scene.remove(this.root);
  }
}

const XR_HAND_BONES = [
  ["wrist","thumb-metacarpal"],["thumb-metacarpal","thumb-phalanx-proximal"],["thumb-phalanx-proximal","thumb-phalanx-distal"],["thumb-phalanx-distal","thumb-tip"],
  ["wrist","index-finger-metacarpal"],["index-finger-metacarpal","index-finger-phalanx-proximal"],["index-finger-phalanx-proximal","index-finger-phalanx-intermediate"],["index-finger-phalanx-intermediate","index-finger-phalanx-distal"],["index-finger-phalanx-distal","index-finger-tip"],
  ["wrist","middle-finger-metacarpal"],["middle-finger-metacarpal","middle-finger-phalanx-proximal"],["middle-finger-phalanx-proximal","middle-finger-phalanx-intermediate"],["middle-finger-phalanx-intermediate","middle-finger-phalanx-distal"],["middle-finger-phalanx-distal","middle-finger-tip"],
  ["wrist","ring-finger-metacarpal"],["ring-finger-metacarpal","ring-finger-phalanx-proximal"],["ring-finger-phalanx-proximal","ring-finger-phalanx-intermediate"],["ring-finger-phalanx-intermediate","ring-finger-phalanx-distal"],["ring-finger-phalanx-distal","ring-finger-tip"],
  ["wrist","pinky-finger-metacarpal"],["pinky-finger-metacarpal","pinky-finger-phalanx-proximal"],["pinky-finger-phalanx-proximal","pinky-finger-phalanx-intermediate"],["pinky-finger-phalanx-intermediate","pinky-finger-phalanx-distal"],["pinky-finger-phalanx-distal","pinky-finger-tip"],
];
const XR_HAND_JOINTS = [...new Set(XR_HAND_BONES.flat())];
const FINGER_COLORS = {
  thumb:0xff6b6b, index:0xffd166, middle:0x58d6bd, ring:0x62a8ff, pinky:0xc77dff,
};
function fingerColor(name) {
  if(name.startsWith("thumb"))return FINGER_COLORS.thumb;
  if(name.startsWith("index"))return FINGER_COLORS.index;
  if(name.startsWith("middle"))return FINGER_COLORS.middle;
  if(name.startsWith("ring"))return FINGER_COLORS.ring;
  if(name.startsWith("pinky"))return FINGER_COLORS.pinky;
  return 0xffffff;
}

function circleTexture() {
  const canvas=document.createElement("canvas"); canvas.width=canvas.height=64;
  const context=canvas.getContext("2d");
  context.clearRect(0,0,64,64); context.fillStyle="#fff";
  context.beginPath(); context.arc(32,32,27,0,Math.PI*2); context.fill();
  return new THREE.CanvasTexture(canvas);
}

function axisGizmo() {
  const group=new THREE.Group(), length=0.12, shaft=0.003, tip=0.016;
  [[0xff4d5a,"x"],[0x45e58b,"y"],[0x4d9fff,"z"]].forEach(([color,axis]) => {
    const material=new THREE.MeshBasicMaterial({ color, depthTest:false, depthWrite:false });
    const cylinder=new THREE.Mesh(new THREE.CylinderGeometry(shaft,shaft,length-tip,10),material);
    const cone=new THREE.Mesh(new THREE.ConeGeometry(tip*0.45,tip,12),material.clone());
    if(axis==="x") { cylinder.rotation.z=-Math.PI/2; cone.rotation.z=-Math.PI/2; cylinder.position.x=(length-tip)/2; cone.position.x=length-tip/2; }
    else if(axis==="y") { cylinder.position.y=(length-tip)/2; cone.position.y=length-tip/2; }
    else { cylinder.rotation.x=Math.PI/2; cone.rotation.x=Math.PI/2; cylinder.position.z=(length-tip)/2; cone.position.z=length-tip/2; }
    cylinder.renderOrder=cone.renderOrder=1000003; group.add(cylinder,cone);
  });
  const center=new THREE.Mesh(
    new THREE.SphereGeometry(0.012,12,8),
    new THREE.MeshBasicMaterial({ color:0xffffff, depthTest:false, depthWrite:false }),
  );
  center.renderOrder=1000004; group.add(center); return group;
}

export class SpatialXrOverlay {
  constructor(scene) {
    this.scene = scene;
    this.root = new THREE.Group();
    this.root.visible = false;
    this.scene.add(this.root);
    this.hands = {};
    [["left",0x58d6bd],["right",0xf4a261]].forEach(([side,color]) => {
      const markerTexture=circleTexture();
      const markers=new THREE.Group();
      XR_HAND_JOINTS.forEach(name => {
        const material=new THREE.SpriteMaterial({ map:markerTexture, color:fingerColor(name), depthTest:false, depthWrite:false, transparent:true });
        const marker=new THREE.Sprite(material); marker.userData.joint=name; marker.scale.setScalar(0.018); marker.renderOrder=1000004; markers.add(marker);
      });
      const bones=new THREE.Group();
      XR_HAND_BONES.forEach(([,end]) => {
        const material=new THREE.MeshBasicMaterial({ color:fingerColor(end), depthTest:false, depthWrite:false });
        const bone=new THREE.Mesh(new THREE.CylinderGeometry(0.0035,0.0035,1,8),material);
        bone.userData.end=end; bone.renderOrder=1000003; bones.add(bone);
      });
      const axes=axisGizmo();
      markers.renderOrder=bones.renderOrder=axes.renderOrder=1000002;
      this.root.add(markers,bones,axes);
      this.hands[side]={ markers, markerTexture, bones, axes };
    });
  }

  setEnabled(enabled) { this.root.visible = !!enabled; }

  update(hands) {
    if (!this.root.visible) return;
    Object.entries(this.hands).forEach(([side, visual]) => {
      const hand = hands[side];
      const isHand = hand?.tracked && hand.source_type === "hand" && Object.keys(hand.joints || {}).length;
      visual.markers.visible = visual.bones.visible = !!isHand;
      visual.axes.visible = !!hand?.tracked;
      if (visual.axes.visible) {
        const pose=hand;
        visual.axes.position.set(pose.position.x,pose.position.y,pose.position.z);
        visual.axes.quaternion.set(pose.orientation.x,pose.orientation.y,pose.orientation.z,pose.orientation.w);
      }
      if (isHand) {
        visual.markers.children.forEach(marker => {
          const joint=hand.joints[marker.userData.joint]; marker.visible=!!joint;
          if(joint) marker.position.set(joint.position.x,joint.position.y,joint.position.z);
        });
        XR_HAND_BONES.forEach(([a,b],index) => {
          const first=hand.joints[a]?.position, second=hand.joints[b]?.position;
          const bone=visual.bones.children[index]; bone.visible=!!(first&&second); if(!bone.visible)return;
          const start=new THREE.Vector3(first.x,first.y,first.z), end=new THREE.Vector3(second.x,second.y,second.z);
          const delta=end.clone().sub(start), length=delta.length();
          bone.position.copy(start).addScaledVector(delta,0.5); bone.scale.set(1,length,1);
          bone.quaternion.setFromUnitVectors(new THREE.Vector3(0,1,0),delta.normalize());
        });
      }
    });
  }

  dispose() {
    Object.values(this.hands).forEach(({markers,markerTexture,bones,axes}) => {
      markers.children.forEach(marker => marker.material.dispose()); markerTexture.dispose();
      bones.children.forEach(bone => { bone.geometry.dispose(); bone.material.dispose(); });
      axes.traverse(object => { object.geometry?.dispose(); object.material?.dispose(); });
    });
    this.scene.remove(this.root);
  }
}

export class NativeQuadUI {
  constructor(renderer, session, layer, binding, viewerSpace, callbacks) {
    this.nativeQuad = true;
    this.renderer = renderer;
    this.session = session;
    this.layer = layer;
    this.binding = binding;
    this.viewerSpace = viewerSpace;
    this.callbacks = callbacks;
    this.canvas = document.createElement("canvas");
    this.canvas.width = 1024;
    this.canvas.height = 640;
    this.context = this.canvas.getContext("2d");
    this.cards = [];
    this.hits = new Map();
    this.drag = null;
    this.lastDraw = 0;
    this.locked = false;
    this.syncCards();
    this.onSelectStart = event => this.selectStart(event.inputSource);
    this.onSelectEnd = event => { if (this.drag?.source === event.inputSource) this.drag = null; };
    session.addEventListener("selectstart", this.onSelectStart);
    session.addEventListener("selectend", this.onSelectEnd);
  }

  syncCards() {
    this.cards.forEach(item => item.panel.dispose());
    const nodes = [...document.querySelectorAll("#workspace > .tile")];
    const gap = 12, columns = 2, cellWidth = (this.canvas.width - gap * 3) / columns;
    const cellHeight = (this.canvas.height - gap * 4) / 3;
    this.cards = nodes.map((card, index) => ({
      panel: new SpatialCard(card, index),
      rect: {
        x: gap + (index % columns) * (cellWidth + gap),
        y: gap + Math.floor(index / columns) * (cellHeight + gap),
        width: cellWidth,
        height: cellHeight,
      },
    }));
    return this.cards.length;
  }

  rayHit(source, frame) {
    const pose = frame.getPose(source.targetRaySpace, this.viewerSpace);
    if (!pose) return null;
    const p = pose.transform.position, q = pose.transform.orientation;
    const origin = new THREE.Vector3(p.x, p.y, p.z);
    const direction = new THREE.Vector3(0, 0, -1)
      .applyQuaternion(new THREE.Quaternion(q.x, q.y, q.z, q.w)).normalize();
    if (Math.abs(direction.z) < 1e-5) return null;
    const distance = (-1.5 - origin.z) / direction.z;
    if (distance <= 0) return null;
    const point = origin.addScaledVector(direction, distance);
    const u = (point.x + 0.8) / 1.6;
    const v = 0.5 - point.y;
    if (u < 0 || u > 1 || v < 0 || v > 1) return null;
    const x = u * this.canvas.width, y = v * this.canvas.height;
    const item = [...this.cards].reverse().find(value => x >= value.rect.x && x <= value.rect.x + value.rect.width && y >= value.rect.y && y <= value.rect.y + value.rect.height);
    return { x, y, item };
  }

  update(frame, _viewer, time) {
    for (const source of this.session.inputSources) {
      if (!source.targetRaySpace) continue;
      const hit = this.rayHit(source, frame);
      this.hits.set(source, hit);
      if (this.drag?.source === source && hit) {
        this.drag.item.rect.x = THREE.MathUtils.clamp(hit.x - this.drag.offsetX, 0, this.canvas.width - this.drag.item.rect.width);
        this.drag.item.rect.y = THREE.MathUtils.clamp(hit.y - this.drag.offsetY, 0, this.canvas.height - this.drag.item.rect.height);
      }
    }
    if (time - this.lastDraw < 66 && !this.drag) return;
    this.draw(time);
    this.upload(frame);
    this.lastDraw = time;
  }

  draw(time) {
    const context = this.context;
    context.clearRect(0, 0, this.canvas.width, this.canvas.height);
    for (const item of this.cards) {
      item.panel.draw(time);
      context.drawImage(item.panel.canvas, item.rect.x, item.rect.y, item.rect.width, item.rect.height);
      context.strokeStyle = "#58d6bd";
      context.lineWidth = 3;
      context.strokeRect(item.rect.x, item.rect.y, item.rect.width, item.rect.height);
    }
    for (const hit of this.hits.values()) {
      if (!hit) continue;
      context.beginPath(); context.arc(hit.x, hit.y, 9, 0, Math.PI * 2);
      context.strokeStyle = "#7ae4cf"; context.lineWidth = 4; context.stroke();
    }
  }

  upload(frame) {
    const subImage = this.binding.getSubImage(this.layer, frame);
    const gl = this.renderer.getContext();
    gl.bindTexture(gl.TEXTURE_2D, subImage.colorTexture);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, true);
    gl.texSubImage2D(gl.TEXTURE_2D, 0, 0, 0, gl.RGBA, gl.UNSIGNED_BYTE, this.canvas);
    gl.pixelStorei(gl.UNPACK_FLIP_Y_WEBGL, false);
    gl.bindTexture(gl.TEXTURE_2D, null);
    this.renderer.resetState();
  }

  selectStart(source) {
    const hit = this.hits.get(source);
    if (!hit?.item) return;
    const { item } = hit;
    const localX = (hit.x - item.rect.x) / item.rect.width * item.panel.canvas.width;
    const localY = (hit.y - item.rect.y) / item.rect.height * item.panel.canvas.height;
    if (localY <= NATIVE_HEADER_PX) {
      this.drag = { source, item, offsetX:hit.x-item.rect.x, offsetY:hit.y-item.rect.y };
      return;
    }
    const action = item.panel.hitAction({ x:localX/item.panel.canvas.width, y:1-localY/item.panel.canvas.height });
    if (action === "lock") this.callbacks.lock();
    if (action === "exit") this.callbacks.exit();
  }

  setLocked(locked) {
    this.locked = locked;
    if ("visible" in this.layer) this.layer.visible = !locked;
  }

  debugState() {
    return { nativeQuad:true, cards:this.cards.length, width:this.layer.width, height:this.layer.height };
  }

  dispose() {
    this.session.removeEventListener("selectstart", this.onSelectStart);
    this.session.removeEventListener("selectend", this.onSelectEnd);
    this.cards.forEach(item => item.panel.dispose());
    if ("destroy" in this.layer) this.layer.destroy();
  }
}

export async function createNativeQuadUI(renderer, session, callbacks) {
  const binding = renderer.xr.getBinding();
  const projectionLayer = renderer.xr.getBaseLayer();
  if (!binding || !projectionLayer || typeof binding.createQuadLayer !== "function") return null;
  const viewerSpace = await session.requestReferenceSpace("viewer");
  const layer = binding.createQuadLayer({
    space: viewerSpace,
    transform: new XRRigidTransform({ x:0, y:0, z:-1.5 }),
    viewPixelWidth: 1024,
    viewPixelHeight: 640,
    width: 1.6,
    height: 1.0,
    layout: "mono",
    isStatic: false,
  });
  session.updateRenderState({ layers:[projectionLayer, layer] });
  return new NativeQuadUI(renderer, session, layer, binding, viewerSpace, callbacks);
}

export function createXrRenderer(canvas, opaque = false) {
  // PICO Browser 4 / Chromium 125 exposes XRWebGLBinding, but its projection
  // framebuffer is incomplete with recent Three.js. Force the proven
  // XRWebGLLayer backend while WebXRManager detects platform capabilities.
  const bindingDescriptor = Object.getOwnPropertyDescriptor(globalThis, "XRWebGLBinding");
  let bindingHidden = false;
  try {
    if (!bindingDescriptor || bindingDescriptor.configurable) {
      Object.defineProperty(globalThis, "XRWebGLBinding", { configurable:true, writable:true, value:undefined });
      bindingHidden = true;
    } else if (bindingDescriptor?.writable) {
      globalThis.XRWebGLBinding = undefined;
      bindingHidden = true;
    }
    const renderer = new THREE.WebGLRenderer({ canvas, alpha: !opaque, antialias: false, powerPreference: "high-performance" });
    renderer.xr.enabled = true;
    renderer.xr.setFramebufferScaleFactor(0.8);
    renderer.setSize(Math.max(innerWidth, 2), Math.max(innerHeight, 2), false);
    renderer.setClearColor(0x020405, opaque ? 1 : 0);
    const scene = new THREE.Scene();
    const camera = new THREE.PerspectiveCamera(70, 1, 0.01, 20);
    return { renderer, scene, camera };
  } finally {
    if (bindingHidden) {
      if (bindingDescriptor) Object.defineProperty(globalThis, "XRWebGLBinding", bindingDescriptor);
      else delete globalThis.XRWebGLBinding;
    }
  }
}
