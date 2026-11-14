import {arrivalProgress} from './arrival-progress.js';
import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';
import { RoomEnvironment } from 'three/addons/environments/RoomEnvironment.js';
import { RoundedBoxGeometry } from 'three/addons/geometries/RoundedBoxGeometry.js';

const PALETTE = { ready: 0xffffff, occupied: 0xa9dcff, attention: 0xffb08a, cleaning: 0xd6c2ff, unknown: 0xe4e3e8 };
const WING_COLORS = [0xf3cfdc, 0xf5e2b8, 0xdcd2f5, 0xcadff3];
const BLANKETS = [0xff8fb1, 0xffc65c, 0x9fd0ff, 0x7cb8ff, 0xc39bff, 0xff9f6e];
const ROOM_WIDTH = 3.2, ROOM_DEPTH = 3.6, FLOOR_Y = .27;
const WALL_HEIGHT = .9, WALL_THICKNESS = .2, WALL_CENTER_Y = FLOOR_Y + WALL_HEIGHT / 2;
const DAY_MS = 86400000;

export function createHotelScene(container, onSelect, onHover = () => {}, onUnpin = () => {}) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#f3f2f0');
  scene.fog = new THREE.Fog('#f3f2f0', 110, 220);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = .95;
  // Soft studio reflections give every surface gentle, professional shading.
  const pmrem = new THREE.PMREMGenerator(renderer);
  scene.environment = pmrem.fromScene(new RoomEnvironment(), .04).texture; scene.environmentIntensity = .55;
  container.appendChild(renderer.domElement);
  renderer.domElement.setAttribute('aria-label', 'Single-level illustrative layout of PLATO 40: all 40 rooms, reception and housekeeping. Original room numbers are preserved; this is not a measured floor plan. Drag to rotate, right-drag to pan, scroll to zoom, or select a room. Figures illustrate known occupied rooms; their positions and activities are not measured.');
  const camera = new THREE.PerspectiveCamera(37, 1, .1, 320);
  camera.position.set(22, 48, 55);
  const controls = new OrbitControls(camera, renderer.domElement);
  controls.target.set(0, .7, 2.8);
  controls.enableDamping = true;
  controls.dampingFactor = .07;
  controls.minDistance = 6;
  controls.maxDistance = 170;
  // No azimuth bounds: every side of the open-roof building is accessible.
  controls.minPolarAngle = .06;
  controls.maxPolarAngle = Math.PI * .485;
  controls.enablePan = true;
  controls.screenSpacePanning = true;
  controls.autoRotateSpeed = .35;
  controls.update();
  scene.add(new THREE.HemisphereLight(0xffffff, 0xcfcbd4, .75));
  const sun = new THREE.DirectionalLight(0xfff1e0, 2.4);
  sun.position.set(-20, 40, 22);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  Object.assign(sun.shadow.camera, { left: -29, right: 29, top: 29, bottom: -29, near: 1, far: 95 });
  sun.shadow.normalBias = .03; sun.shadow.bias = -.0004; sun.shadow.radius = 3;
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0xdfe9ff, .55);
  fill.position.set(15, 16, -16); scene.add(fill);
  const rim = new THREE.DirectionalLight(0xffffff, .7); rim.position.set(24, 22, -30); scene.add(rim);

  const geometries = new Set(), materials = new Set(), textures = new Set(), geometryCache = new Map(), materialCache = new Map(), zoneLabels = [];
  function geometry(key, create) {
    if (!geometryCache.has(key)) { const value = create(); geometryCache.set(key, value); geometries.add(value); }
    return geometryCache.get(key);
  }
  function material(color, options = {}) {
    const key = JSON.stringify([color, options]);
    if (!materialCache.has(key)) { const value = new THREE.MeshStandardMaterial({ color, roughness: .83, ...options }); materialCache.set(key, value); materials.add(value); }
    return materialCache.get(key);
  }
  const chalk = material(0xfbfaf8, { roughness: .9 }), plaster = material(0xeeece8), oak = material(0xe6cfb0, { roughness: .7 }), walnut = material(0xc6a27c, { roughness: .7 });
  const dark = material(0x5b5470), linen = material(0xffffff), green = material(0x8fd19e), brass = material(0xb69c66, { metalness: .4, roughness: .42 });
  const frost = material(0xe8ebef, { transparent: true, opacity: .48, roughness: .75, depthWrite: false, side: THREE.DoubleSide });
  const glass = material(0xdfe6ee, { transparent: true, opacity: .16, roughness: .22, depthWrite: false, side: THREE.DoubleSide });
  function mesh(parent, shape, mat, x = 0, y = 0, z = 0, architectural = true) {
    const value = new THREE.Mesh(shape, mat); value.position.set(x, y, z);
    value.castShadow = architectural; value.receiveShadow = true; value.userData.architectural = architectural;
    parent.add(value); return value;
  }
  function box(parent, w, h, d, mat, x = 0, y = 0, z = 0, architectural = true) {
    // Softly bevelled blocks catch the light like a product render; tiny parts stay plain boxes.
    const r = Math.min(.045, Math.min(w, h, d) * .3);
    return mesh(parent, geometry(`b:${w}:${h}:${d}`, () => r > .012 ? new RoundedBoxGeometry(w, h, d, 2, r) : new THREE.BoxGeometry(w, h, d)), mat, x, y, z, architectural);
  }
  function cylinder(parent, radius, height, mat, x, y, z, top = radius, architectural = true) {
    return mesh(parent, geometry(`c:${radius}:${top}:${height}`, () => new THREE.CylinderGeometry(top, radius, height, 10)), mat, x, y, z, architectural);
  }
  function orb(parent, radius, mat, x, y, z, architectural = true) {
    return mesh(parent, geometry(`s:${radius}`, () => new THREE.SphereGeometry(radius, 10, 7)), mat, x, y, z, architectural);
  }
  // Static furniture is one mesh per material per wing, rather than thousands
  // of separate draw calls. Interactive tiles, hit targets and residents stay separate.
  function bakeArchitecture(parent) {
    parent.updateWorldMatrix(true, true);
    const inverse = new THREE.Matrix4().copy(parent.matrixWorld).invert(), buckets = new Map(), source = [];
    parent.traverse(item => {
      if (!item.isMesh || !item.userData.architectural) return;
      const key = item.material.uuid;
      if (!buckets.has(key)) buckets.set(key, { material: item.material, parts: [], items: [] });
      const copy = item.geometry.index ? item.geometry.toNonIndexed() : item.geometry.clone();
      for (const name of Object.keys(copy.attributes)) if (!['position', 'normal', 'uv'].includes(name)) copy.deleteAttribute(name);
      copy.applyMatrix4(new THREE.Matrix4().multiplyMatrices(inverse, item.matrixWorld));
      buckets.get(key).parts.push(copy); buckets.get(key).items.push(item); source.push(item);
    });
    const keep = new Set();
    for (const { material: mat, parts, items } of buckets.values()) {
      const combined = mergeGeometries(parts, false); parts.forEach(part => part.dispose());
      if (!combined) { items.forEach(item => keep.add(item)); continue; } // keep originals rather than lose them
      geometries.add(combined);
      const item = new THREE.Mesh(combined, mat); item.castShadow = !mat.transparent; item.receiveShadow = true; parent.add(item);
    }
    source.forEach(item => { if (!keep.has(item)) item.removeFromParent(); });
  }
  function textTexture(text, width = 256, height = 96) {
    const canvas = document.createElement('canvas'); canvas.width = width; canvas.height = height;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = '#f2eee3'; ctx.fillRect(0, 0, width, height);
    ctx.font = `600 ${Math.min(52, width / Math.max(1, text.length * .65))}px sans-serif`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillStyle = '#3d554a';
    ctx.fillText(text, width / 2, height / 2 + 2);
    const texture = new THREE.CanvasTexture(canvas); texture.colorSpace = THREE.SRGBColorSpace; textures.add(texture); return texture;
  }
  function label(parent, text, x, y, z, width = .46, height = .17) {
    const mat = new THREE.MeshBasicMaterial({ map: textTexture(text), side: THREE.DoubleSide }); materials.add(mat);
    return mesh(parent, geometry(`label:${width}:${height}`, () => new THREE.PlaneGeometry(width, height)), mat, x, y, z);
  }
  function zoneLabel(parent, text, width) {
    const mat = new THREE.SpriteMaterial({ map: textTexture(text, 512, 96), depthTest: false, depthWrite: false }); materials.add(mat);
    const value = new THREE.Sprite(mat); value.position.set(0, 2.6, 0); value.scale.set(width, width * 96 / 512, 1);
    value.renderOrder = 3; parent.add(value); zoneLabels.push(value); return value;
  }

  const ground = mesh(scene, geometry('ground', () => new THREE.PlaneGeometry(180, 180)), material(0xf3f2f0), 0, -.38, 0, false);
  ground.rotation.x = -Math.PI / 2;
  const landscape = new THREE.Group(); scene.add(landscape);
  // Courtyard hotel: one square plinth, an entrance path to reception in the south-west corner.
  box(landscape, 28, .3, 35, chalk, 0, -.17, 3.4);
  box(landscape, 28.7, .12, 35.7, plaster, 0, -.34, 3.4);
  box(landscape, 3, .035, 3.6, material(0xe4e1db), -6, .005, 22.6);
  bakeArchitecture(landscape);

  const building = new THREE.Group(); scene.add(building);
  const floors = [], hitTargets = [], roomsById = new Map(), residents = [], zones = new Map();
  const clothes = [0x6a6f8a, 0xb57a57, 0x71869d, 0x997797], skinColors = [0xdbb396, 0xb78768, 0x9c6b50, 0xe8c5a5];
  // Clean, slim, anime-proportioned residents (original design): jointed limbs, smooth head, sharp hair.
  const TOPS = [0x2f3442, 0xf4f1ec, 0x8a2f3b, 0x3d5a80, 0x5b4b6e, 0xc9a66b, 0x23272f, 0x7a8798];
  const BOTTOMS = [0x1f2229, 0x3a3f4b, 0x2b2a33, 0x4a4f5c];
  const HAIR = [0x16151a, 0x2b1f1a, 0xe9dcc2, 0x5a2d22, 0xcfd3dc, 0x1d2a44, 0x8b5a36, 0xd9a05b, 0x6b3a5c, 0xb24a3a];
  const smooth = (key, make) => geometry(key, make);
  function part(parent, geo, mat, x, y, z) { return mesh(parent, geo, mat, x, y, z, false); }
  function limb(parent, x, y, radius, length, mat) {
    const pivot = new THREE.Group(); pivot.position.set(x, y, 0); parent.add(pivot);
    part(pivot, smooth(`limb:${radius}:${length}`, () => new THREE.CapsuleGeometry(radius, length, 6, 14)), mat, 0, -length / 2 - radius * .4, 0);
    return pivot;
  }
  // Chibi residents (original design): big round head, small body, stubby limbs, clean smooth shading.
  const HEAD_R = .26;
  const pick = (seed, salt, n) => (((seed + 7) * 2654435761 ^ salt * 40503) >>> 0) % n;
  function hair(head, seed, mat) {
    const R = HEAD_R, style = pick(seed, 3, 11);
    const capGeo = (k, cut) => smooth(`hair-cap:${k}:${cut}`, () => new THREE.SphereGeometry(R * k, 32, 20, 0, Math.PI * 2, 0, Math.PI * cut));
    const blade = (x, y, z, rx, rz, len, w = .26) => { const b = part(head, smooth(`hair-blade:${len}:${w}`, () => new THREE.ConeGeometry(R * w, len, 5)), mat, x, y, z); b.rotation.set(rx, 0, rz); b.scale.z = .55; return b; };
    const ball = (r, x, y, z) => part(head, smooth(`hair-ball:${r}`, () => new THREE.SphereGeometry(r, 24, 16)), mat, x, y, z);
    if (style !== 6 && style !== 10) { const cap = part(head, capGeo(1.06, .58), mat, 0, R * .1, -R * .04); cap.scale.set(1.03, 1.02, 1.05); }
    if (style === 0) for (let k = -2; k <= 2; k++) blade(k * R * .3, R * .48, R * .82, 2.45, k * .14, R * .5);                 // straight fringe
    if (style === 1) { const back = part(head, smooth('hair-long', () => new THREE.CapsuleGeometry(R * .82, R * .7, 8, 18)), mat, 0, -R * .45, -R * .42); back.scale.set(1.05, 1, .55); } // long
    if (style === 2) for (let k = 0; k < 7; k++) { const a = -1.2 + k * .4; blade(Math.sin(a) * R * .9, R * .78, -Math.cos(a) * R * .35, -.45 - Math.cos(a) * .35, -Math.sin(a) * .9, R * .62); } // spiky
    if (style === 3) { for (let k = -2; k <= 2; k++) blade(k * R * .3, R * .48, R * .82, 2.45, k * .14, R * .45); const tail = part(head, smooth('hair-tail', () => new THREE.CapsuleGeometry(R * .28, R * .7, 6, 12)), mat, 0, -R * .2, -R * 1.02); tail.rotation.x = .5; } // ponytail
    if (style === 4) { const bob = part(head, capGeo(1.1, .74), mat, 0, R * .02, -R * .05); bob.scale.set(1.04, 1.05, 1.04); } // bob
    if (style === 5) { for (const side of [-1, 1]) blade(side * R * .72, R * .25, R * .55, 2.75, side * .4, R * .7); blade(0, R * 1.08, R * .1, -.35, 0, R * .45); } // side-swept + ahoge
    if (style === 6) { const buzz = part(head, capGeo(1.015, .55), mat, 0, R * .05, 0); buzz.scale.set(1, 1, 1.02); } // buzz cut
    if (style === 7) { for (const side of [-1, 1]) ball(R * .36, side * R * .72, R * .78, -R * .15); } // twin buns
    if (style === 8) { const fro = ball(R * 1.28, 0, R * .32, -R * .12); fro.scale.set(1, .92, 1); } // afro
    if (style === 9) { for (const side of [-1, 1]) blade(side * R * .45, R * .2, R * .8, 2.9, side * .55, R * .9, .32); } // middle-part curtains
    if (style === 10) { const hat = part(head, capGeo(1.1, .5), material(0x3d5a80, { roughness: .8 }), 0, R * .16, 0); hat.scale.set(1, 1.05, 1); const rim = part(head, smooth('beanie-rim', () => new THREE.TorusGeometry(R * 1.02, R * .12, 10, 32)), material(0x3d5a80, { roughness: .8 }), 0, R * .3, 0); rim.rotation.x = Math.PI / 2; ball(R * .2, 0, R * 1.2, 0).material = material(0xf4f1ec); } // beanie
  }
  function drawFace(head, seed) {
    const R = HEAD_R, eyeStyle = pick(seed, 5, 5), mouth = pick(seed, 9, 4), extra = pick(seed, 13, 6);
    const ink = material(0x18161c, { roughness: .25 }), shine = material(0xffffff, { roughness: .2, emissive: 0xffffff, emissiveIntensity: .4 });
    const z = R * .9;
    for (const side of [-1, 1]) {
      const x = side * .09;
      if (eyeStyle === 0) { part(head, smooth('eye-oval', () => new THREE.SphereGeometry(.034, 16, 12)), ink, x, -.01, z).scale.set(1, 1.35, .45); part(head, smooth('eye-shine', () => new THREE.SphereGeometry(.011, 10, 8)), shine, x + .01, .02, z + .01); }
      if (eyeStyle === 1) { const arc = part(head, smooth('eye-happy', () => new THREE.TorusGeometry(.03, .009, 6, 14, Math.PI)), ink, x, -.015, z + .01); }       // ^ ^
      if (eyeStyle === 2) { part(head, smooth('eye-sleepy', () => new THREE.BoxGeometry(.06, .011, .01)), ink, x, -.005, z + .012); }                               // - -
      if (eyeStyle === 3) { part(head, smooth('eye-big', () => new THREE.SphereGeometry(.046, 18, 14)), ink, x, -.02, z - .005).scale.set(1, 1.3, .45); part(head, smooth('eye-shine-big', () => new THREE.SphereGeometry(.015, 10, 8)), shine, x + .014, .015, z + .012); }
      if (eyeStyle === 4) { part(head, smooth('eye-dot', () => new THREE.SphereGeometry(.022, 12, 10)), ink, x, -.01, z + .005).scale.set(1, 1, .5); }
    }
    if (mouth === 1) { const m = part(head, smooth('mouth-smile', () => new THREE.TorusGeometry(.032, .008, 6, 14, Math.PI)), ink, 0, -.085, z - .01); m.rotation.z = Math.PI; }
    if (mouth === 2) part(head, smooth('mouth-o', () => new THREE.TorusGeometry(.016, .007, 6, 14)), ink, 0, -.09, z - .015);
    if (mouth === 3) part(head, smooth('mouth-flat', () => new THREE.BoxGeometry(.045, .008, .01)), ink, 0, -.088, z - .012);
    if (extra === 0) for (const side of [-1, 1]) { const ring = part(head, smooth('glasses', () => new THREE.TorusGeometry(.05, .008, 8, 20)), ink, side * .09, -.01, z + .03); } // glasses
    if (extra === 1) for (const side of [-1, 1]) part(head, smooth('blush', () => new THREE.SphereGeometry(.03, 12, 8)), material(0xf3a6b4, { roughness: .6 }), side * .15, -.07, z - .04).scale.set(1.2, .6, .3);
    if (extra === 2) for (const side of [-1, 1]) for (const [dx, dy] of [[0, 0], [.025, .015], [.035, -.012]]) part(head, smooth('freckle', () => new THREE.SphereGeometry(.007, 6, 5)), material(0xa86a4c), side * (.13 + dx), -.05 + dy, z - .02);
  }
  function resident(parent, seed) {
    const root = new THREE.Group(); parent.add(root); root.visible = false; root.userData.illustrative = true;
    const rig = new THREE.Group(); root.add(rig);
    const top = material(TOPS[pick(seed, 11, TOPS.length)], { roughness: .65 }), pants = material(BOTTOMS[pick(seed, 17, BOTTOMS.length)], { roughness: .7 });
    const skin = material(skinColors[pick(seed, 19, skinColors.length)], { roughness: .5 }), shoe = material(0x1a1a1f, { roughness: .4 });
    const legs = [-1, 1].map(side => { const leg = limb(rig, side * .085, .27, .07, .12, pants); part(leg, smooth('c-shoe', () => new THREE.SphereGeometry(.075, 18, 12)), shoe, 0, -.23, .025).scale.set(1, .62, 1.35); return leg; });
    const torso = part(rig, smooth('c-body', () => new THREE.CapsuleGeometry(.2, .2, 10, 22)), top, 0, .5, 0);
    torso.scale.set(1.05, 1, .9); torso.castShadow = true;
    const arms = [-1, 1].map(side => { const arm = limb(rig, side * .235, .66, .058, .16, top); part(arm, smooth('c-hand', () => new THREE.SphereGeometry(.062, 18, 12)), skin, 0, -.26, 0); return arm; });
    const head = new THREE.Group(); head.position.set(0, .72 + HEAD_R * .9, 0); rig.add(head);
    const face = part(head, smooth('c-head', () => new THREE.SphereGeometry(HEAD_R, 40, 28)), skin, 0, 0, 0); face.scale.set(1, .96, .94); face.castShadow = true;
    drawFace(head, seed);
    hair(head, seed, material(HAIR[pick(seed, 7, HAIR.length)], { roughness: .4 }));
    const book = new THREE.Group(); book.position.set(0, .55, .26); book.rotation.x = -.6; rig.add(book);
    box(book, .2, .025, .15, material(0x8a2f3b), 0, 0, 0, false);
    const value = { root, rig, torso, head, arms, legs, book, seed, activity: seed % 4, occupied: false };
    root.userData.person = value;
    residents.push(value); return value;
  }

  for (let level = 0; level < 4; level++) {
    // Four wings share one plane. Room IDs retain their dataset identity even
    // though the presentation deliberately does not reproduce physical floors.
    const floor = new THREE.Group();
    // Pinwheel plan (16.4 × 9.2 wings): outer square 25.6 m, compact 7.2 m courtyard in the middle.
    const ring = [[-4.6, -8.2, 0], [8.2, -4.6, -Math.PI / 2], [4.6, 8.2, Math.PI], [-8.2, 4.6, Math.PI / 2]][level];
    floor.position.set(ring[0], 0, ring[1]); floor.rotation.y = ring[2];
    floor.userData.wing = level + 1; floor.userData.illustrative = true;
    building.add(floor); floors.push(floor);
    box(floor, 16.45, .2, 9.2, chalk, 0, .12, 0);
    box(floor, 16.48, .055, 9.22, plaster, 0, .03, 0);
    box(floor, 16, .025, 1.35, material(0xc3bdac), 0, .235, 0);
    const wingLabel = label(floor, `${level + 1}01 — ${level + 1}10`, 0, .27, 0, 2.1, .38);
    wingLabel.rotation.x = -Math.PI / 2;
    for (let index = 0; index < 10; index++) {
      const column = index % 5, back = index >= 5, seed = level * 10 + index;
      const id = `R${level + 1}${String(index + 1).padStart(2, '0')}`;
      const group = new THREE.Group(); group.position.set((column - 2) * ROOM_WIDTH, 0, back ? -2.6 : 2.6);
      group.userData.room = id; group.userData.illustrative = true;
      if (back) group.rotation.y = Math.PI; floor.add(group);
      const tileMaterial = new THREE.MeshStandardMaterial({ color: PALETTE.unknown, roughness: .92 }); materials.add(tileMaterial);
      const tile = box(group, 3.02, .035, 3.42, tileMaterial, 0, .245, 0, false); tile.userData.room = id;
      // Pick the architectural floor, not an invisible room-height box that
      // could intercept a click aimed at the room behind it in a cutaway view.
      hitTargets.push(tile);
      // Sims-style cutaway: low, open walls so the room life stays visible.
      const LOW = .62, LOW_Y = FLOOR_Y + LOW / 2, cap = material(WING_COLORS[level]);
      for (const x of [-1.5, 1.5]) { box(group, WALL_THICKNESS, LOW, ROOM_DEPTH, chalk, x, LOW_Y, 0); box(group, WALL_THICKNESS + .02, .06, ROOM_DEPTH, cap, x, FLOOR_Y + LOW + .03, 0); }
      box(group, 2.05, LOW, WALL_THICKNESS, chalk, -.575, LOW_Y, -1.7); box(group, 2.07, .06, WALL_THICKNESS + .02, cap, -.575, FLOOR_Y + LOW + .03, -1.7);
      box(group, .27, LOW, WALL_THICKNESS, chalk, 1.465, LOW_Y, -1.7); box(group, .29, .06, WALL_THICKNESS + .02, cap, 1.465, FLOOR_Y + LOW + .03, -1.7);
      box(group, ROOM_WIDTH, LOW, WALL_THICKNESS, chalk, 0, LOW_Y, 1.7); box(group, ROOM_WIDTH + .02, .06, WALL_THICKNESS + .02, cap, 0, FLOOR_Y + LOW + .03, 1.7);
      const floorNumber = label(group, id.slice(1), -.7, .275, -1.42, .72, .25); floorNumber.rotation.x = -Math.PI / 2;
      const occupancyMaterial = new THREE.MeshStandardMaterial({ color: 0xa7aca6, roughness: .55 }); materials.add(occupancyMaterial);
      const occupancy = cylinder(group, .075, .025, occupancyMaterial, .25, 1.65, -1.8, .075, false); occupancy.visible = false;
      // Bed, layered linen, book desk and reading chair.
      box(group, 1.42, .27, 1.91, oak, -.55, .42, -.15);
      box(group, 1.37, .19, 1.85, linen, -.55, .65, -.15);
      box(group, 1.4, .05, .9, material(BLANKETS[seed % BLANKETS.length]), -.55, .77, .33);
      box(group, 1.48, .45, .09, material(WING_COLORS[level]), -.55, .6, -1.13);
      box(group, .37, .47, .4, walnut, .45, .51, -.9);
      box(group, .67, .065, .55, oak, 1.02, .98, .4);
      for (const x of [.77, 1.27]) box(group, .045, .68, .045, dark, x, .62, .4);
      box(group, .43, .1, .43, green, .94, .62, 1.08);
      box(group, .43, .45, .07, green, .94, .88, 1.28);
      for (const x of [.78, 1.1]) box(group, .035, .31, .035, dark, x, .42, 1.09);
      box(group, .48, .52, .24, material(seed % 2 ? 0x8b8578 : 0xb6a28a), -.88, .53, -1.47);
      const figure = resident(group, seed);
      roomsById.set(id, { group, tile, occupancy, figure, floor: level + 1 });
    }
    bakeArchitecture(floor);
  }

  // Shared circulation stays open: each room doorway leads onto its wing
  // corridor, then into the central spine and the reception lobby.
  const shared = new THREE.Group(); shared.userData.illustrative = true; building.add(shared);
  const stone = material(0xd8d6c8), carpet = material(0xb7b7a2), terracotta = material(0xbc886c);
  box(shared, 7.4, .2, 7.4, chalk, 0, .1, 0);                           // compact courtyard
  box(shared, 7, .027, 7, stone, 0, .215, 0);
  box(shared, 5.2, .03, 4.6, carpet, 0, .235, 0);                         // lounge rug
  box(shared, 27, .2, 7.6, chalk, 0, .1, 16.8);                           // south front: reception + housekeeping
  box(shared, 26.4, .027, 7, stone, 0, .215, 16.8);

  function sofa(parent, x, z, rotation = 0, mat = green) {
    const value = new THREE.Group(); value.position.set(x, FLOOR_Y, z); value.rotation.y = rotation; parent.add(value);
    box(value, 2.5, .34, .8, mat, 0, .3, 0);
    box(value, 2.5, .64, .18, mat, 0, .65, -.36);
    for (const side of [-1, 1]) box(value, .18, .52, .9, mat, side * 1.2, .48, 0);
    for (const cx of [-.78, 0, .78]) box(value, .72, .15, .68, linen, cx, .53, .02);
    return value;
  }
  const reception = new THREE.Group(); reception.position.set(-6, 0, 16.2); reception.userData.zone = 'reception'; reception.userData.illustrative = true; shared.add(reception); zones.set('reception', reception);
  zoneLabel(reception, 'RECEPTION', 5);
  // The desk is empty on purpose: source data does not locate reception staff.
  box(reception, 4.9, .93, .95, dark, 0, .735, 0);
  box(reception, 5.15, .09, 1.15, oak, 0, 1.245, 0);
  for (const x of [-1.35, 1.15]) {
    box(reception, .36, .035, .22, dark, x, 1.315, -.05);
    box(reception, .055, .3, .055, dark, x, 1.46, -.1);
    box(reception, .7, .43, .06, dark, x, 1.69, -.1);
    box(reception, .61, .34, .008, material(0xcfd6dc), x, 1.69, -.137);
    box(reception, .55, .025, .19, walnut, x, 1.315, -.32);
  }
  const deskSign = label(reception, 'PLATO 40', 0, .94, .515, 1.45, .28);
  const lobbySign = label(shared, 'RECEPTION', -6, .28, 18.9, 3.3, .48); lobbySign.rotation.x = -Math.PI / 2;
    sofa(shared, 0, -1.4, 0, terracotta); sofa(shared, 0, 1.4, Math.PI, material(0x9fb4c8));
  cylinder(shared, .7, .065, oak, 0, .79, 0);
  cylinder(shared, .11, .49, dark, 0, .51, 0);
  for (const [x, z] of [[-2.2, -1.4], [2.2, 1.4]]) {
    box(shared, 1.1, .13, .65, walnut, x, .45, z);
    for (const dx of [-.4, .4]) box(shared, .07, .22, .48, dark, x + dx, .32, z);
    box(shared, .46, .65, .27, terracotta, x - .18, .83, z);
  }

  const housekeeping = new THREE.Group(); housekeeping.position.set(7.6, 0, 16.8); housekeeping.userData.zone = 'housekeeping'; housekeeping.userData.illustrative = true; shared.add(housekeeping); zones.set('housekeeping', housekeeping);
  zoneLabel(housekeeping, 'HOUSEKEEPING', 5.7);
  box(housekeeping, 8.1, .035, 5.5, material(0xe6e6ea), 0, .263, 0);
  for (const x of [-4, 4]) box(housekeeping, WALL_THICKNESS, WALL_HEIGHT, 5.6, chalk, x, WALL_CENTER_Y, 0);
  box(housekeeping, 8.1, WALL_HEIGHT, WALL_THICKNESS, chalk, 0, WALL_CENTER_Y, -2.7);
  box(housekeeping, 6, WALL_HEIGHT, WALL_THICKNESS, chalk, -1.1, WALL_CENTER_Y, 2.7);
  box(housekeeping, .9, WALL_HEIGHT, WALL_THICKNESS, chalk, 3.6, WALL_CENTER_Y, 2.7);
  const utilitySign = label(housekeeping, 'HOUSEKEEPING', -.9, .29, 1.9, 3.4, .42); utilitySign.rotation.x = -Math.PI / 2;
  // Open roof keeps the linen shelves and supplies visible from above.
  for (const x of [-2.85, -.85, 1.15]) {
    box(housekeeping, 1.7, .9, .57, oak, x, .72, -2.21);
  }
  for (const x of [-2.85, -1.6]) {
    box(housekeeping, 1.04, 1.07, .9, linen, x, .805, -.65);
  }
  for (const x of [.4, 2.4]) {
    const cart = new THREE.Group(); cart.position.set(x, FLOOR_Y, .25); housekeeping.add(cart);
    for (const y of [.2, .75]) box(cart, .9, .065, .67, walnut, 0, y, 0);
    for (const dx of [-.4, .4]) for (const dz of [-.28, .28]) {
      cylinder(cart, .025, .85, brass, dx, .5, dz);
      cylinder(cart, .08, .06, dark, dx, .08, dz).rotation.z = Math.PI / 2;
    }
    for (let stack = 0; stack < 4; stack++) box(cart, .62, .07, .45, linen, 0, .28 + stack * .075, 0);
  }
  bakeArchitecture(shared);

  const selection = new THREE.Group(); scene.add(selection); selection.visible = false;
  const selectionMaterial = new THREE.MeshBasicMaterial({ color: 0x1a1718, transparent: true, opacity: .95 }); materials.add(selectionMaterial);
  for (const x of [-1.52, 1.52]) box(selection, .055, .035, 3.48, selectionMaterial, x, 0, 0, false);
  for (const z of [-1.74, 1.74]) box(selection, 3.1, .035, .055, selectionMaterial, 0, 0, z, false);

  let selected = null, peopleVisible = true, currentRooms = [], animation, lastFrame = 0, down = null, cameraMode = 'overview';
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let playback = { timeMs: 0, sourceTimeMs: null, playing: true, speed: 1, historical: false, anchoredAt: performance.now() };
  function illustrationTime(now) {
    const elapsed = playback.playing && !reducedMotion ? Math.max(0, now - playback.anchoredAt) * Math.min(8, playback.speed) : 0;
    return (playback.timeMs % DAY_MS + elapsed) / 1000;
  }
  function pose(person, time) {
    const { root, rig, head, arms, legs, book, seed, activity } = person;
    const phase = seed * 1.73, idle = Math.sin(time * .8 + phase), walk = Math.sin(time * 4 + phase);
    root.rotation.set(0, 0, 0); rig.rotation.set(0, 0, 0); rig.position.set(0, 0, 0); root.scale.setScalar(1);
    head.rotation.set(0, idle * .12, 0); book.visible = activity === 1;
    rig.scale.set(1, 1 + Math.sin(time * 1.6 + phase) * .018, 1); // breathing
    arms.forEach((a, k) => a.rotation.set(0, 0, k ? -.06 : .06)); legs.forEach(l => l.rotation.set(0, 0, 0));
    if (activity === 0) { // asleep on the bed
      root.position.set(-.55, .98, -.45); rig.rotation.x = -Math.PI / 2; head.rotation.set(0, .2, 0);
    } else if (activity === 1) { // reading in the armchair
      root.position.set(.94, .42, 1.05); root.rotation.y = Math.PI; head.rotation.x = .25;
      legs.forEach(l => l.rotation.x = -Math.PI / 2); arms.forEach(a => a.rotation.x = -.9);
    } else if (activity === 2) { // at the window, weight shifting
      root.position.set(.3, FLOOR_Y, 1.22); rig.rotation.z = Math.sin(time * .5 + phase) * .02;
      arms[1].rotation.x = -.4 + idle * .05;
    } else { // walking a short loop
      const route = Math.sin(time * .5 + phase);
      root.position.set(.75 + Math.cos(time * .5 + phase) * .25, FLOOR_Y + Math.abs(walk) * .03, -.25 + route * .55);
      root.rotation.y = Math.cos(time * .5 + phase) >= 0 ? 0 : Math.PI;
      const speed = Math.min(1, Math.abs(Math.cos(time * .5 + phase)) * 1.3 + .25);
      legs.forEach((l, k) => l.rotation.x = walk * (k ? -.6 : .6) * speed); arms.forEach((a, k) => a.rotation.x = walk * (k ? .5 : -.5) * speed);
    }
  }
  function refresh() {
    const states = new Map(currentRooms.map(room => [room.id, room]));
    for (const [id, value] of roomsById) {
      const room = states.get(id), occupied = room?.occupancy === 'occupied';
      value.figure.occupied = occupied;
      value.figure.root.visible = peopleVisible && occupied;
      value.figure.name = room?.guest_name || 'Guest'; value.figure.subtitle = `Room ${id.slice(1)}`;

      value.tile.material.color.setHex(PALETTE[room?.active_request_count ? 'attention' : room?.status] || PALETTE.unknown);
      value.tile.material.emissive.setHex(id === selected ? 0x4986b4 : 0);
      value.tile.material.emissiveIntensity = id === selected ? .15 : 0;
      value.occupancy.material.color.setHex(occupied ? 0x4986b4 : room?.occupancy === 'vacant' ? 0xf2f2f4 : 0xa7aca6);
      value.occupancy.material.emissive.setHex(occupied ? 0x23465f : 0);
      value.occupancy.material.emissiveIntensity = .2;
    }
    const value = roomsById.get(selected);
    if (value) {
      value.group.getWorldPosition(selection.position); selection.position.y += .29; { const q = new THREE.Quaternion(); value.group.getWorldQuaternion(q); selection.rotation.y = new THREE.Euler().setFromQuaternion(q, "YXZ").y; }
      selection.visible = true;
    } else selection.visible = false;
  }
  // Big emoji speech bubbles above rooms with something going on.
  const bubbles = new Map();
  const BUBBLE_EMOJI = { water_issue: '💧', temperature_cold: '🥶', temperature_hot: '🥵', access_issue: '🔑', extra_towels: '🧺', cleaning_request: '🧹', wifi_help: '📶', noise_complaint: '🔊', maintenance_issue: '🔧', early_checkin: '⏰', late_checkout: '🛏️', waiting_for_room: '⌛', arrival_delay: '🚕', luggage_storage: '🧳', parking: '🚗', invoice: '🧾', air_quality: '🌬️', unknown: '💬' };
  function bubbleTexture(intent, pending) {
    const emoji = BUBBLE_EMOJI[intent] || BUBBLE_EMOJI.unknown, tone = pending ? '#bd8bad' : '#ff8a5c';
    const canvas = document.createElement('canvas'); canvas.width = 256; canvas.height = 236;
    const ctx = canvas.getContext('2d');
    ctx.fillStyle = 'rgba(26,23,24,.18)'; ctx.beginPath(); ctx.roundRect(14, 18, 228, 168, 64); ctx.fill();
    ctx.fillStyle = '#ffffff'; ctx.strokeStyle = tone; ctx.lineWidth = 10;
    ctx.beginPath(); ctx.roundRect(10, 8, 228, 168, 64); ctx.fill(); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(96, 170); ctx.lineTo(128, 226); ctx.lineTo(150, 170); ctx.closePath(); ctx.fill();
    ctx.beginPath(); ctx.moveTo(96, 176); ctx.lineTo(128, 226); ctx.lineTo(150, 176); ctx.stroke();
    ctx.fillStyle = '#ffffff'; ctx.fillRect(100, 160, 46, 16);
    ctx.font = '112px "Apple Color Emoji","Segoe UI Emoji","Noto Color Emoji",sans-serif'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(emoji, 124, 96);
    if (pending) { ctx.fillStyle = tone; ctx.beginPath(); ctx.arc(214, 28, 22, 0, Math.PI * 2); ctx.fill(); ctx.fillStyle = '#fff'; ctx.font = '700 30px system-ui,sans-serif'; ctx.fillText('!', 214, 30); }
    const texture = new THREE.CanvasTexture(canvas); texture.colorSpace = THREE.SRGBColorSpace; textures.add(texture); return texture;
  }
  function setBubbles(list) {
    const wanted = new Map((list || []).map(item => [item.room, item]));
    for (const [room, bubble] of bubbles) {
      const next = wanted.get(room);
      if (!next || next.intent !== bubble.intent || next.pending !== bubble.pending) { bubble.sprite.parent?.remove(bubble.sprite); bubble.sprite.material.map?.dispose(); bubble.sprite.material.dispose(); bubbles.delete(room); }
    }
    for (const [room, item] of wanted) {
      if (bubbles.has(room)) continue;
      const host = roomsById.get(room); if (!host) continue;
      const sprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: bubbleTexture(item.intent, item.pending), depthTest: false, transparent: true }));
      sprite.scale.set(1.9, 1.75, 1); sprite.renderOrder = 20; sprite.position.set(0, 2.6, 0);
      host.group.add(sprite); sprite.scale.set(.01, .01, 1); bubbles.set(room, { sprite, intent: item.intent, pending: item.pending, phase: Math.random() * 6, born: performance.now() });
    }
  }
  // Staff on duty (illustrative positions): uniforms by role, gentle work loops.
  const staffGroup = new THREE.Group(); scene.add(staffGroup);
  const staff = new Map();
  const UNIFORM = {
    housekeeping: { top: 0xb9a7e6, pants: 0x3a3f4b }, maintenance: { top: 0xf08a3c, pants: 0x34405a },
    reception: { top: 0x22324f, pants: 0x1f2229 }, technician: { top: 0xf08a3c, pants: 0x34405a },
  };
  function robotFigure() {
    const root = new THREE.Group();
    const body = mesh(root, smooth('robot-body', () => new THREE.CapsuleGeometry(.24, .22, 8, 20)), material(0xf4f4f6, { roughness: .35, metalness: .1 }), 0, .4, 0, false); body.castShadow = true;
    const visor = mesh(root, smooth('robot-visor', () => new THREE.SphereGeometry(.17, 24, 16, 0, Math.PI * 2, 0, Math.PI * .5)), material(0x23465f, { roughness: .2 }), 0, .62, .06, false); visor.rotation.x = Math.PI / 2.4;
    const light = mesh(root, smooth('robot-light', () => new THREE.SphereGeometry(.035, 12, 8)), material(0x9fe8ff, { emissive: 0x4fc3f7, emissiveIntensity: 1 }), 0, .86, 0, false);
    return { root, rig: root, light, head: visor, arms: [], legs: [], robot: true };
  }
  function staffFigure(person, seed) {
    const role = String(person.role || '').toLowerCase();
    if (role.includes('robot')) return robotFigure();
    const fig = resident(staffGroup, seed); residents.pop(); fig.root.visible = true;
    const u = UNIFORM[role] || UNIFORM[role.includes('house') || role.includes('clean') ? 'housekeeping' : role.includes('tech') || role.includes('maint') ? 'maintenance' : 'reception'];
    const top = material(u.top, { roughness: .6 }), pants = material(u.pants, { roughness: .7 });
    fig.torso.material = top; fig.arms.forEach(a => { a.children[0].material = top; }); fig.legs.forEach(l => { l.children[0].material = pants; });
    if (role.includes('house') || role.includes('clean')) {
      const apron = part(fig.rig, smooth('apron', () => new THREE.CylinderGeometry(.205, .23, .3, 22, 1, true, -.9, 1.8)), material(0xffffff, { roughness: .7, side: THREE.DoubleSide }), 0, .45, 0); apron.scale.z = .92;
      const band = part(fig.head, smooth('headband', () => new THREE.TorusGeometry(HEAD_R * 1.01, .022, 8, 32)), material(0xffffff), 0, HEAD_R * .45, 0); band.rotation.x = Math.PI / 2 - .25;
      const towels = new THREE.Group(); towels.position.set(.2, .48, .2); fig.rig.add(towels);
      for (let k = 0; k < 3; k++) box(towels, .2, .045, .15, material(k % 2 ? 0xf4f1ec : 0xcfe0f5), 0, k * .05, 0, false);
    } else if (role.includes('maint') || role.includes('tech')) {
      const helmet = part(fig.head, smooth('helmet', () => new THREE.SphereGeometry(HEAD_R * 1.12, 28, 16, 0, Math.PI * 2, 0, Math.PI * .5)), material(0xffd23f, { roughness: .35 }), 0, HEAD_R * .12, 0);
      const brim = part(fig.head, smooth('helmet-brim', () => new THREE.CylinderGeometry(HEAD_R * 1.25, HEAD_R * 1.25, .03, 28)), material(0xffd23f, { roughness: .35 }), 0, HEAD_R * .12, .03);
      const kit = box(fig.arms[1], .2, .14, .1, material(0xc0392b, { roughness: .5 }), 0, -.33, 0, false);
      for (const side of [-1, 1]) part(fig.rig, smooth('strap', () => new THREE.BoxGeometry(.035, .32, .02)), material(0x34405a), side * .09, .55, .19);
    } else {
      part(fig.rig, smooth('collar', () => new THREE.ConeGeometry(.09, .1, 3)), material(0xffffff), 0, .66, .15).rotation.x = Math.PI;
      part(fig.rig, smooth('nametag', () => new THREE.BoxGeometry(.07, .035, .01)), material(0xd8b45a, { metalness: .5, roughness: .3 }), .1, .6, .2);
    }
    return fig;
  }
  function staffAnchor(person, role) {
    const room = person.room_id && roomsById.get(person.room_id);
    if (room) { const p = new THREE.Vector3(); room.group.getWorldPosition(p); return { kind: 'room', base: p, room }; }
    if (role.includes('recep')) { const p = new THREE.Vector3(); zones.get('reception').getWorldPosition(p); return { kind: 'desk', base: p.add(new THREE.Vector3(0, FLOOR_Y, -.95)) }; }
    if (role.includes('house') || role.includes('clean')) { const p = new THREE.Vector3(); zones.get('housekeeping').getWorldPosition(p); return { kind: 'linen', base: p.add(new THREE.Vector3(0, FLOOR_Y, .9)) }; }
    return { kind: 'patrol', base: new THREE.Vector3(0, FLOOR_Y, 0) };
  }
  function setStaff(people) {
    const list = (people || []).filter(p => !['guest', 'visitor'].includes(p.role));
    const keep = new Set(list.map(p => p.id));
    for (const [id, s] of staff) if (!keep.has(id)) { staffGroup.remove(s.fig.root); staff.delete(id); }
    list.forEach((person, i) => {
      const role = String(person.role || '').toLowerCase();
      let s = staff.get(person.id);
      if (!s) { const fig = staffFigure(person, 50 + i * 7); fig.root.userData.person = fig; staffGroup.add(fig.root); s = { fig, phase: i * 1.9 }; staff.set(person.id, s); }
      s.fig.name = person.name || person.id; s.fig.subtitle = role.replaceAll('_', ' ').replace(/^./, c => c.toUpperCase());
      s.anchor = staffAnchor(person, role); s.role = role;
    });
  }
  function staffPose(s, time) {
    const { fig, anchor, phase } = s, t = time + phase;
    const r = fig.root; r.rotation.set(0, 0, 0);
    // Walk cycle scaled by real speed: legs move whenever the body moves, stand still at turns.
    const stride = (speed) => { const w = Math.sin(t * 6.5); fig.arms.forEach((a, i) => a.rotation.set(w * (i ? .5 : -.5) * speed, 0, 0)); fig.legs.forEach((l, i) => l.rotation.set(w * (i ? -.65 : .65) * speed, 0, 0)); return Math.abs(w) * .035 * speed; };
    const pace = (rate, span, x0, y0, z0, axis = 'x') => {
      const p = Math.sin(t * rate), v = Math.cos(t * rate), speed = Math.min(1, Math.abs(v) * 1.4);
      const bob = stride(speed);
      if (axis === 'x') r.position.set(x0 + p * span, y0 + bob, z0); else r.position.set(x0, y0 + bob, z0 + p * span);
      r.rotation.y = axis === 'x' ? (v >= 0 ? Math.PI / 2 : -Math.PI / 2) : (v >= 0 ? 0 : Math.PI);
      if (speed < .15) fig.head.rotation.y = Math.sin(t * .8) * .4; else fig.head.rotation.y = 0;
    };
    if (fig.robot) { const a = t * .3; r.position.set(Math.cos(a) * 3, FLOOR_Y + .02 + Math.sin(t * 8) * .01, Math.sin(a) * 3); r.rotation.y = -a; r.rotation.z = Math.sin(t * 8) * .02; fig.light.material.emissiveIntensity = .6 + Math.abs(Math.sin(t * 3)) * .8; return; }
    if (anchor.kind === 'desk') { // works at the desk, steps between the two screens now and then
      const shift = Math.sin(t * .22), moving = Math.abs(Math.cos(t * .22)) > .55;
      r.position.set(anchor.base.x + Math.sign(shift) * Math.min(1, Math.abs(shift) * 1.6) * .7, anchor.base.y, anchor.base.z);
      if (moving) { r.position.y += stride(.8); r.rotation.y = Math.cos(t * .22) > 0 ? Math.PI / 2 : -Math.PI / 2; }
      else { fig.legs.forEach(l => l.rotation.set(0, 0, 0)); fig.arms.forEach((a, i) => a.rotation.set(-.9 + Math.sin(t * 9 + i) * .08, 0, 0)); fig.head.rotation.y = Math.sin(t * .5) * .25; }
      return;
    }
    if (anchor.kind === 'linen') return pace(.35, 2.6, anchor.base.x, anchor.base.y, anchor.base.z);
    if (anchor.kind === 'room') return pace(.5, .5, anchor.base.x, FLOOR_Y, anchor.base.z);
    return pace(.2, 7, -4.6, FLOOR_Y, -8.2); // patrols the north wing corridor
  }
  const pinCanvas = document.createElement('canvas'); pinCanvas.width = pinCanvas.height = 160;
  { const ctx = pinCanvas.getContext('2d');
    ctx.fillStyle = 'rgba(26,23,24,.2)'; ctx.beginPath(); ctx.arc(82, 84, 64, 0, Math.PI * 2); ctx.fill();
    ctx.fillStyle = '#1a1718'; ctx.beginPath(); ctx.arc(80, 78, 64, 0, Math.PI * 2); ctx.fill();
    ctx.strokeStyle = '#fff'; ctx.lineWidth = 9; ctx.lineCap = 'round'; ctx.lineJoin = 'round';
    ctx.beginPath(); ctx.moveTo(62, 44); ctx.lineTo(98, 44); ctx.lineTo(93, 74); ctx.lineTo(108, 89); ctx.lineTo(108, 98); ctx.lineTo(52, 98); ctx.lineTo(52, 89); ctx.lineTo(67, 74); ctx.closePath(); ctx.stroke();
    ctx.beginPath(); ctx.moveTo(80, 100); ctx.lineTo(80, 122); ctx.stroke(); }
  const pinTexture = new THREE.CanvasTexture(pinCanvas); pinTexture.colorSpace = THREE.SRGBColorSpace; textures.add(pinTexture);
  const pinSprite = new THREE.Sprite(new THREE.SpriteMaterial({ map: pinTexture, depthTest: false, transparent: true }));
  pinSprite.scale.set(.85, .85, 1); pinSprite.renderOrder = 25; pinSprite.visible = false; scene.add(pinSprite);
  function setPinned(id) {
    selected = id && roomsById.has(id) ? id : null; refresh();
    const host = selected && roomsById.get(selected);
    if (host) { host.group.getWorldPosition(pinSprite.position); pinSprite.position.y = 1.75; pinSprite.userData.born = performance.now(); }
    pinSprite.visible = Boolean(host);
    if (hovered === selected) hoverBox.visible = false;
  }
  const raycaster = new THREE.Raycaster(), pointer = new THREE.Vector2();
  function point(event) {
    const rect = renderer.domElement.getBoundingClientRect(); if (!rect.width || !rect.height) return null;
    pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    const first = raycaster.intersectObject(building, true).find(hit => hit.object.visible && !hit.object.isSprite);
    const id = first && roomAt(first.point);
    return id ? { object: roomsById.get(id).tile, point: first.point } : null;
  }
  const roomFrames = new Map();
  function roomYaw(group) { const q = new THREE.Quaternion(); group.getWorldQuaternion(q); return new THREE.Euler().setFromQuaternion(q, 'YXZ').y; }
  function roomAt(p) {
    if (!roomFrames.size) for (const [id, v] of roomsById) { const c = new THREE.Vector3(); v.group.getWorldPosition(c); roomFrames.set(id, { c, yaw: roomYaw(v.group) }); }
    if (p.y > 2.2) return null;
    for (const [id, { c, yaw }] of roomFrames) {
      const dx = p.x - c.x, dz = p.z - c.z, lx = dx * Math.cos(yaw) - dz * Math.sin(yaw), lz = dx * Math.sin(yaw) + dz * Math.cos(yaw);
      if (Math.abs(lx) < 1.5 && Math.abs(lz) < 1.7) return id;
    }
    return null; // corridors, courtyard, lobby, housekeeping: not a room
  }
  // People: hover outline + name tag.
  const outlineMaterial = new THREE.MeshBasicMaterial({ color: 0x4986b4, side: THREE.BackSide }); materials.add(outlineMaterial);
  function outline(fig, on) {
    if (!fig) return;
    if (on && !fig.outlined) { fig.outlined = []; fig.root.traverse(m => { if (m.isMesh && !m.userData.outline) { const o = new THREE.Mesh(m.geometry, outlineMaterial); o.userData.outline = true; o.scale.setScalar(1.09); o.raycast = () => {}; m.add(o); fig.outlined.push(o); } }); }
    fig.outlined?.forEach(o => { o.visible = on; });
  }
  const tagCanvas = document.createElement('canvas'); tagCanvas.width = 512; tagCanvas.height = 112;
  const tagTexture = new THREE.CanvasTexture(tagCanvas); tagTexture.colorSpace = THREE.SRGBColorSpace; textures.add(tagTexture);
  const nameTag = new THREE.Sprite(new THREE.SpriteMaterial({ map: tagTexture, depthTest: false, transparent: true })); nameTag.renderOrder = 30; nameTag.visible = false; scene.add(nameTag);
  function drawTag(name, sub) {
    const ctx = tagCanvas.getContext('2d'); ctx.clearRect(0, 0, 512, 112);
    ctx.font = '600 38px "Manrope Variable", system-ui, sans-serif'; const w1 = ctx.measureText(name).width;
    ctx.font = '400 26px "Manrope Variable", system-ui, sans-serif'; const w2 = sub ? ctx.measureText(sub).width + 18 : 0;
    const w = Math.min(500, w1 + w2 + 56), x = (512 - w) / 2;
    ctx.fillStyle = 'rgba(26,23,24,.88)'; ctx.beginPath(); ctx.roundRect(x, 18, w, 76, 38); ctx.fill();
    ctx.fillStyle = '#ffffff'; ctx.font = '600 38px "Manrope Variable", system-ui, sans-serif'; ctx.textBaseline = 'middle'; ctx.fillText(name, x + 28, 57);
    if (sub) { ctx.fillStyle = 'rgba(255,255,255,.62)'; ctx.font = '400 26px "Manrope Variable", system-ui, sans-serif'; ctx.fillText(sub, x + 28 + w1 + 18, 58); }
    tagTexture.needsUpdate = true; nameTag.scale.set(2.6, .57, 1);
  }
  let hoveredPerson = null;
  function personAt(event) {
    const roots = [...residents.filter(r => r.root.visible).map(r => r.root), ...[...staff.values()].map(s => s.fig.root)];
    const hit = raycaster.intersectObjects(roots, true)[0]; if (!hit) return null;
    let o = hit.object; while (o && !o.userData.person) o = o.parent; return o?.userData.person || null;
  }
  function setPersonHover(fig) {
    if (fig === hoveredPerson) return;
    outline(hoveredPerson, false); hoveredPerson = fig; outline(fig, true);
    nameTag.visible = Boolean(fig); if (fig) drawTag(fig.name || 'Guest', fig.subtitle || '');
  }
  const pointerDown = event => { down = [event.clientX, event.clientY]; };
  const pointerUp = event => {
    const origin = down; down = null;
    if (!origin || Math.hypot(event.clientX - origin[0], event.clientY - origin[1]) > 5) return;
    // The pin unpins; a click inside a room pins it; halls, lobby and empty space change nothing.
    const hit = point(event);
    if (pinSprite.visible && raycaster.intersectObject(pinSprite).length) { onUnpin(); return; }
    if (personAt(event)) return;
    const id = hit?.object.userData.room; if (id && roomsById.has(id) && id !== selected) onSelect(id);
  };
  // Room boundary on hover: glowing wall-top frame, corner posts, floor edge and a faint floor tint.
  const hoverMaterial = new THREE.MeshBasicMaterial({ color: 0x4986b4, transparent: true, opacity: .85, depthTest: false });
  const hoverFill = new THREE.MeshBasicMaterial({ color: 0x8cc3ee, transparent: true, opacity: .22, depthWrite: false });
  materials.add(hoverMaterial); materials.add(hoverFill);
  const hoverBox = new THREE.Group(); hoverBox.visible = false; hoverBox.renderOrder = 15; scene.add(hoverBox);
  const W = 3.12, D = 3.52, TOP = FLOOR_Y + .62 + .08;
  for (const y of [FLOOR_Y + .03, TOP]) {
    for (const x of [-W / 2, W / 2]) { const m = box(hoverBox, .06, .04, D, hoverMaterial, x, y, 0, false); m.renderOrder = 15; }
    for (const z of [-D / 2, D / 2]) { const m = box(hoverBox, W + .06, .04, .06, hoverMaterial, 0, y, z, false); m.renderOrder = 15; }
  }
  for (const x of [-W / 2, W / 2]) for (const z of [-D / 2, D / 2]) { const m = box(hoverBox, .05, TOP - FLOOR_Y, .05, hoverMaterial, x, (TOP + FLOOR_Y) / 2, z, false); m.renderOrder = 15; }
  const tint = new THREE.Mesh(new THREE.PlaneGeometry(W, D), hoverFill); tint.rotation.x = -Math.PI / 2; tint.position.y = FLOOR_Y + .02; hoverBox.add(tint);
  let hovered = null;
  function setHover(id) {
    if (id === hovered) return; hovered = id;
    const host = id && id !== selected ? roomsById.get(id) : null;
    if (host) { host.group.getWorldPosition(hoverBox.position); hoverBox.rotation.y = roomYaw(host.group); }
    hoverBox.visible = Boolean(host); onHover(id);
  }
  const pointerMove = event => { const hit = down ? null : point(event); const person = down ? null : personAt(event); const onPin = !down && pinSprite.visible && raycaster.intersectObject(pinSprite).length > 0; renderer.domElement.style.cursor = hit || person || onPin ? 'pointer' : 'grab'; setHover(person ? null : hit?.object.userData.room || null); setPersonHover(person); };
  const pointerLeave = () => { setHover(null); setPersonHover(null); };
  const pointerCancel = () => { down = null; };
  renderer.domElement.addEventListener('pointerdown', pointerDown);
  renderer.domElement.addEventListener('pointerup', pointerUp);
  renderer.domElement.addEventListener('pointermove', pointerMove);
  renderer.domElement.addEventListener('pointerleave', pointerLeave);
  renderer.domElement.addEventListener('pointercancel', pointerCancel);
  let incidentCamera = null, lastIncidentAt = -Infinity;
  const overviewTarget = new THREE.Vector3(0, .75, 3), overviewDirection = new THREE.Vector3(.55, 2.1, 1.25).normalize();
  function overview() {
    incidentCamera = null;
    cameraMode = 'overview';
    zoneLabels.forEach(value => { value.visible = true; });
    // Fit the actual one-level property in both axes. A fixed camera distance
    // clips wings on narrow displays and wastes space on the wide dashboard.
    const right = new THREE.Vector3().crossVectors(new THREE.Vector3(0, 1, 0), overviewDirection).normalize();
    const up = new THREE.Vector3().crossVectors(overviewDirection, right).normalize();
    const tanY = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)), tanX = tanY * camera.aspect;
    let distance = 0;
    for (const x of [-14, 14]) for (const y of [-.35, 3]) for (const z of [-14, 21]) {
      const delta = new THREE.Vector3(x, y, z).sub(overviewTarget);
      distance = Math.max(distance, delta.dot(overviewDirection) + Math.abs(delta.dot(right)) / tanX,
        delta.dot(overviewDirection) + Math.abs(delta.dot(up)) / tanY);
    }
    distance *= .9;
    controls.maxDistance = Math.max(170, distance * 1.2); camera.far = Math.max(320, distance + 90); camera.updateProjectionMatrix();
    controls.target.copy(overviewTarget); camera.position.copy(overviewTarget).addScaledVector(overviewDirection, distance); controls.update();
  }
  function focus(group, distance) {
    incidentCamera = null;
    if (!group) return false;
    cameraMode = 'detail';
    zoneLabels.forEach(value => { value.visible = false; });
    const direction = camera.position.clone().sub(controls.target).normalize();
    group.getWorldPosition(controls.target); controls.target.y = .85;
    camera.position.copy(controls.target).addScaledVector(direction, distance); controls.update(); return true;
  }
  const incidentSeen = new Set();
  const arrivals = new Map();
  const manualView = () => { incidentCamera = null; cameraMode = 'manual'; zoneLabels.forEach(value => { value.visible = false; }); };
  controls.addEventListener('start', manualView);
  function resize() {
    const w = container.clientWidth, h = container.clientHeight; if (!w || !h) return;
    renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix();
    if (cameraMode === 'overview') overview();
  }
  const observer = new ResizeObserver(resize); observer.observe(container); resize();
  function showIncident(decisions) {
    if (reducedMotion || !playback.historical || !playback.playing) return;
    for (const item of decisions || []) {
      const d = item.decision || item.decision_record || item;
      const id = d.event_id;
      if (!id || incidentSeen.has(id)) continue;
      incidentSeen.add(id);
      const roomId = (d.affected_assets || []).find(id => roomsById.has(id));
      if (!roomId || !['critical', 'high'].includes(d.priority) || incidentCamera || cameraMode !== 'overview' || performance.now() - lastIncidentAt < 45000) continue;
      const target = new THREE.Vector3(); roomsById.get(roomId).group.getWorldPosition(target); target.y = .85;
      const direction = camera.position.clone().sub(controls.target).normalize();
      incidentCamera = {started: performance.now(), from: camera.position.clone(), targetFrom: controls.target.clone(), target, position: target.clone().addScaledVector(direction, 14)};
      lastIncidentAt = performance.now();
    }
  }
  function draw(now) {
    animation = requestAnimationFrame(draw);
    if (document.hidden || now - lastFrame < 26) return;
    lastFrame = now; const time = illustrationTime(now);
    for (const person of residents) if (person.root.visible && person.root.parent.parent.visible) {
      const arrival = arrivals.get(person);
      if (!arrival) {pose(person, time); continue;}
      // Position follows the authoritative replay clock, including large jumps.
      const {moving, fraction, elapsed} = arrivalProgress(arrival.startedAt, playback.sourceTimeMs, arrival.length);
      let remaining = fraction * arrival.length, index = 0;
      while (index < arrival.points.length - 2 && remaining > arrival.points[index].distanceTo(arrival.points[index + 1])) {
        remaining -= arrival.points[index].distanceTo(arrival.points[index + 1]); index++;
      }
      const from = arrival.points[index], to = arrival.points[index + 1];
      const leg = from.distanceTo(to);
      const walkActivity = person.activity; person.activity = 3; pose(person, moving ? elapsed / 1000 : 0); person.activity = walkActivity;
      person.root.position.lerpVectors(from, to, leg ? Math.min(1, remaining / leg) : 1);
      person.root.position.y = FLOOR_Y;
      person.root.rotation.set(0, moving ? Math.atan2(to.x - from.x, to.z - from.z) : Math.PI, 0);
      person.rig.rotation.set(0, 0, 0); person.rig.position.set(0, 0, 0);
      person.book.visible = false;
      if (!moving) {person.arms.forEach(l=>l.rotation.set(0,0,0));person.legs.forEach(l=>l.rotation.set(0,0,0));}
      if (fraction >= 1) {person.activity = 2; arrivals.delete(person);}

    }
    if (incidentCamera) {
      const t = (now - incidentCamera.started) / 1000;
      if (!playback.playing || t >= 6) {
        camera.position.copy(incidentCamera.from); controls.target.copy(incidentCamera.targetFrom); incidentCamera = null;
      } else {
        let k = t < 1.5 ? t / 1.5 : t < 4.5 ? 1 : (6 - t) / 1.5;
        k = k * k * (3 - 2 * k);
        camera.position.lerpVectors(incidentCamera.from, incidentCamera.position, k);
        controls.target.lerpVectors(incidentCamera.targetFrom, incidentCamera.target, k);
      }
    }
    for (const s of staff.values()) staffPose(s, time);
    if (pinSprite.visible) { const age = Math.min(1, (now - (pinSprite.userData.born || 0)) / 380); const pop = age < 1 ? 1 + 2.7 * Math.pow(age - 1, 3) + 1.7 * Math.pow(age - 1, 2) : 1; const host = roomsById.get(selected); if (host) { host.group.getWorldPosition(pinSprite.position); } pinSprite.position.y = 1.75 + Math.sin(now / 420) * .06; pinSprite.scale.set(.85 * pop, .85 * pop, 1); }
    if (hoverBox.visible) hoverMaterial.opacity = .65 + Math.sin(now / 260) * .2;
    if (hoveredPerson && nameTag.visible) { hoveredPerson.head.getWorldPosition(nameTag.position); nameTag.position.y += .62; }
    for (const bubble of bubbles.values()) { const t = now / 1000 + bubble.phase; bubble.sprite.position.y = 2.6 + Math.sin(t * 2.2) * .1; const age = Math.min(1, (now - bubble.born) / 450), pop = age < 1 ? 1 + 2.7 * Math.pow(age - 1, 3) + 1.7 * Math.pow(age - 1, 2) : 1; const k = pop * (1 + Math.sin(t * 3) * .03); bubble.sprite.scale.set(1.9 * k, 1.75 * k, 1); }
    selectionMaterial.opacity = playback.historical && !playback.playing || reducedMotion ? .9 : .8 + Math.sin(time * 1.7) * .12;
    controls.update(); renderer.render(scene, camera);
  }
  refresh(); animation = requestAnimationFrame(draw);
  return {
    update(rooms, id, {timeMs = playback.sourceTimeMs} = {}) {
      const previous = new Map(currentRooms.map(r => [r.id, r]));
      const next = Array.isArray(rooms) ? rooms : [];
      for (const r of next) {
        const host = roomsById.get(r.id);
        if (!host) continue;
        if (r.occupancy !== 'occupied') arrivals.delete(host.figure);
        else if (!reducedMotion && playback.historical && playback.playing && previous.get(r.id)?.occupancy === 'vacant') {
          const receptionWorld = new THREE.Vector3(); zones.get('reception').getWorldPosition(receptionWorld);
          receptionWorld.add(new THREE.Vector3(0, FLOOR_Y, 1.8));
          const corridor = host.group.parent;
          const roomDoor = host.group.localToWorld(new THREE.Vector3(1.05, FLOOR_Y, -1.7));
          const wingDoor = corridor.worldToLocal(roomDoor.clone());
          const pointsWorld = [receptionWorld,
            new THREE.Vector3(-6, FLOOR_Y, 13.3), new THREE.Vector3(0, FLOOR_Y, 13.3),
            new THREE.Vector3(0, FLOOR_Y, 0),
            corridor.localToWorld(new THREE.Vector3(0, FLOOR_Y, 0)),
            corridor.localToWorld(new THREE.Vector3(wingDoor.x, FLOOR_Y, 0)), roomDoor,
            host.group.localToWorld(new THREE.Vector3(1.05, FLOOR_Y, -.7)),
            host.group.localToWorld(new THREE.Vector3(.3, FLOOR_Y, .2)),
            host.group.localToWorld(new THREE.Vector3(.3, FLOOR_Y, 1.22))];
          const points = pointsWorld.map(point=>host.figure.root.parent.worldToLocal(point.clone()));
          let length=0; for(let i=1;i<points.length;i++)length+=points[i-1].distanceTo(points[i]);
          arrivals.set(host.figure, {points, length, startedAt: Number.isFinite(timeMs) ? timeMs : playback.sourceTimeMs});
        }
      }
      currentRooms = next; if (id) selected = id; refresh();
    },
    showIncident,
    setBubbles,
    setStaff,
    setPinned,
    hover(id) { const quiet = onHover; onHover = () => {}; setHover(id); onHover = quiet; },
    // timeMs anchors a deterministic pose on a seek/restart, not every clock
    // update. Normal accelerated replay advances a separate 8x-bounded visual
    // clock; resident motion is never claimed to be a measured source stream.
    setPlayback({ timeMs, playing = true, speed = 1, historical = false, reason } = {}) {
      const now = performance.now(), current = illustrationTime(now) * 1000;
      const sourceTime = Number.isFinite(timeMs) ? timeMs : playback.sourceTimeMs;
      const reset = historical !== playback.historical || ['seek', 'restart', 'step', 'load'].includes(reason)
        || (!playback.playing && sourceTime !== playback.sourceTimeMs);
      if (reset) {arrivals.clear(); if (incidentCamera) {camera.position.copy(incidentCamera.from); controls.target.copy(incidentCamera.targetFrom); incidentCamera = null;} incidentSeen.clear();}
      playback = { timeMs: reset && sourceTime !== null ? sourceTime : current,
        sourceTimeMs: sourceTime, playing: Boolean(playing),
        speed: Number.isFinite(speed) ? Math.max(0, speed) : 1, historical: Boolean(historical), anchoredAt: now };
    },
    showPeople(value) { peopleVisible = Boolean(value); residents.forEach(person => { person.root.visible = peopleVisible && person.occupied; }); },
    select(id) { selected = id; refresh(); },
    focusRoom(id) { return focus(roomsById.get(id)?.group, 10); },
    focusZone(id) { return focus(zones.get(id), id === 'housekeeping' ? 17 : 16); },
    // Legacy callers can no longer hide part of the one-level hotel.
    floor() { floors.forEach(floor => { floor.visible = true; }); refresh(); },
    reset() { overview(); },
    tour(enabled) { controls.autoRotate = Boolean(enabled) && !reducedMotion; },
    destroy() {
      cancelAnimationFrame(animation); observer.disconnect(); controls.removeEventListener('start', manualView); controls.dispose();
      renderer.domElement.removeEventListener('pointerdown', pointerDown); renderer.domElement.removeEventListener('pointerup', pointerUp);
      renderer.domElement.removeEventListener('pointermove', pointerMove); renderer.domElement.removeEventListener('pointercancel', pointerCancel);
      geometries.forEach(value => value.dispose()); materials.forEach(value => value.dispose()); textures.forEach(value => value.dispose());
      renderer.dispose(); renderer.domElement.remove();
    }
  };
}
