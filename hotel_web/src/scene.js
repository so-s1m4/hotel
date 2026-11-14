import * as THREE from 'three';
import { OrbitControls } from 'three/addons/controls/OrbitControls.js';
import { mergeGeometries } from 'three/addons/utils/BufferGeometryUtils.js';

const PALETTE = { ready: 0x99b4a0, occupied: 0xb6cbd0, attention: 0xe7b779, cleaning: 0xb4a8ca, unknown: 0xc8ccc6 };
const ROOM_WIDTH = 3.2, ROOM_DEPTH = 3.6, FLOOR_Y = .27;
const WALL_HEIGHT = 2.6, WALL_THICKNESS = .2, WALL_CENTER_Y = FLOOR_Y + WALL_HEIGHT / 2;
const DAY_MS = 86400000;

export function createHotelScene(container, onSelect) {
  const scene = new THREE.Scene();
  scene.background = new THREE.Color('#eef0e9');
  scene.fog = new THREE.Fog('#eef0e9', 105, 210);
  const renderer = new THREE.WebGLRenderer({ antialias: true });
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1;
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
  scene.add(new THREE.HemisphereLight(0xfffdf5, 0x91a18c, 1.9));
  const sun = new THREE.DirectionalLight(0xfff5df, 2.6);
  sun.position.set(-20, 40, 22);
  sun.castShadow = true;
  sun.shadow.mapSize.set(2048, 2048);
  Object.assign(sun.shadow.camera, { left: -29, right: 29, top: 29, bottom: -29, near: 1, far: 95 });
  sun.shadow.normalBias = .04;
  scene.add(sun);
  const fill = new THREE.DirectionalLight(0xe1ecff, .8);
  fill.position.set(15, 16, -16); scene.add(fill);

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
  const chalk = material(0xf2eee3), plaster = material(0xe4dfd2), oak = material(0xb99570), walnut = material(0x806c52);
  const dark = material(0x43594f), linen = material(0xf6f3e8), green = material(0x7e9479), brass = material(0xb69c66, { metalness: .4, roughness: .42 });
  const frost = material(0xd4e2db, { transparent: true, opacity: .48, roughness: .75, depthWrite: false, side: THREE.DoubleSide });
  const glass = material(0xbfd4cf, { transparent: true, opacity: .16, roughness: .22, depthWrite: false, side: THREE.DoubleSide });
  function mesh(parent, shape, mat, x = 0, y = 0, z = 0, architectural = true) {
    const value = new THREE.Mesh(shape, mat); value.position.set(x, y, z);
    value.castShadow = architectural; value.receiveShadow = true; value.userData.architectural = architectural;
    parent.add(value); return value;
  }
  function box(parent, w, h, d, mat, x = 0, y = 0, z = 0, architectural = true) {
    return mesh(parent, geometry(`b:${w}:${h}:${d}`, () => new THREE.BoxGeometry(w, h, d)), mat, x, y, z, architectural);
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
      if (!buckets.has(key)) buckets.set(key, { material: item.material, parts: [] });
      const copy = item.geometry.clone(); copy.applyMatrix4(new THREE.Matrix4().multiplyMatrices(inverse, item.matrixWorld));
      buckets.get(key).parts.push(copy); source.push(item);
    });
    for (const { material: mat, parts } of buckets.values()) {
      const combined = mergeGeometries(parts, false); parts.forEach(part => part.dispose());
      if (!combined) continue;
      geometries.add(combined);
      const item = new THREE.Mesh(combined, mat); item.castShadow = !mat.transparent; item.receiveShadow = true; parent.add(item);
    }
    source.forEach(item => item.removeFromParent());
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

  const ground = mesh(scene, geometry('ground', () => new THREE.PlaneGeometry(180, 180)), material(0xe5e9df), 0, -.38, 0, false);
  ground.rotation.x = -Math.PI / 2;
  const landscape = new THREE.Group(); scene.add(landscape);
  box(landscape, 40.6, .3, 31.8, chalk, 0, -.17, 3.1);
  box(landscape, 41.3, .12, 32.5, plaster, 0, -.34, 3.1);
  box(landscape, 40, .035, 1.25, material(0xd1d6cb), 0, .005, 18.1);
  for (const [x, z, scale] of [[-21.5,-10,1.35],[-21.3,1,1.05],[-21.5,14,1.2],[21.5,-10,1.2],[21.6,1,1.05],[21.5,14,1.3]]) {
    cylinder(landscape, .1 * scale, 1.9 * scale, oak, x, .95 * scale, z);
    const crown = orb(landscape, .8 * scale, green, x, 2 * scale, z); crown.scale.set(.85, 1.3, .85);
    cylinder(landscape, .6 * scale, .2, plaster, x, .02, z);
  }
  for (const x of [-13, 13]) {
    box(landscape, 2.5, .13, .5, oak, x, .5, 18.2);
    for (const dx of [-.9, .9]) box(landscape, .1, .45, .4, dark, x + dx, .24, 18.2);
  }
  bakeArchitecture(landscape);

  const building = new THREE.Group(); scene.add(building);
  const floors = [], hitTargets = [], roomsById = new Map(), residents = [], zones = new Map();
  const clothes = [0x557869, 0xb57a57, 0x71869d, 0x997797], skinColors = [0xdbb396, 0xb78768, 0x9c6b50, 0xe8c5a5];
  function resident(parent, seed) {
    const root = new THREE.Group(); parent.add(root); root.visible = false; root.userData.illustrative = true;
    const rig = new THREE.Group(); root.add(rig);
    const shirt = material(clothes[seed % clothes.length]), skin = material(skinColors[seed % skinColors.length]);
    const torso = mesh(rig, geometry('torso', () => new THREE.CapsuleGeometry(.145, .27, 3, 7)), shirt, 0, .82, 0, false);
    torso.castShadow = true;
    const head = new THREE.Group(); head.position.set(0, 1.22, 0); rig.add(head);
    orb(head, .14, skin, 0, 0, 0, false).castShadow = true;
    const hair = orb(head, .142, material(seed % 3 ? 0x594536 : 0xbbb7a9), 0, .052, -.018, false); hair.scale.set(1, .65, .92);
    const arms = [], legs = [];
    for (const side of [-1, 1]) {
      const arm = new THREE.Group(); arm.position.set(side * .19, 1, 0); rig.add(arm);
      cylinder(arm, .047, .26, shirt, 0, -.13, 0, .052, false);
      const elbow = new THREE.Group(); elbow.position.y = -.26; arm.add(elbow);
      cylinder(elbow, .037, .24, skin, 0, -.12, 0, .04, false);
      orb(elbow, .043, skin, 0, -.25, 0, false); arms.push({ upper: arm, lower: elbow });
      const leg = new THREE.Group(); leg.position.set(side * .085, .56, 0); rig.add(leg);
      cylinder(leg, .063, .26, dark, 0, -.13, 0, .066, false);
      const knee = new THREE.Group(); knee.position.y = -.26; leg.add(knee);
      cylinder(knee, .05, .25, dark, 0, -.125, 0, .058, false);
      box(knee, .12, .07, .2, walnut, 0, -.255, .035, false); legs.push({ upper: leg, lower: knee });
    }
    const book = new THREE.Group(); book.position.set(0, .8, .28); book.rotation.x = -.36; rig.add(book);
    box(book, .32, .025, .22, material(0xc68c5f), 0, 0, 0, false);
    box(book, .28, .028, .19, linen, 0, .025, 0, false);
    const value = { root, rig, torso, head, arms, legs, book, seed, activity: seed % 4, occupied: false };
    residents.push(value); return value;
  }

  for (let level = 0; level < 4; level++) {
    // Four wings share one plane. Room IDs retain their dataset identity even
    // though the presentation deliberately does not reproduce physical floors.
    const floor = new THREE.Group(); floor.position.set(level % 2 ? 10 : -10, 0, level < 2 ? -6.5 : 6.5);
    floor.userData.wing = level + 1; floor.userData.illustrative = true;
    building.add(floor); floors.push(floor);
    box(floor, 16.45, .2, 9.2, chalk, 0, .12, 0);
    box(floor, 16.48, .055, 9.22, plaster, 0, .03, 0);
    box(floor, 16, .025, 1.35, material(0xc3bdac), 0, .235, 0);
    box(floor, 15.9, .028, .065, brass, 0, .25, -.48);
    box(floor, 15.9, .028, .065, brass, 0, .25, .48);
    box(floor, 1.8, .18, 1.6, chalk, level % 2 ? -8.85 : 8.85, .12, 0);
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
      // Opaque 2.6 m walls, 20 cm thick. Separate side piers and headers leave
      // actual door/window holes; no full wall is hidden behind the glazing.
      for (const x of [-1.5, 1.5]) box(group, WALL_THICKNESS, WALL_HEIGHT, ROOM_DEPTH, chalk, x, WALL_CENTER_Y, 0);
      box(group, 2.05, WALL_HEIGHT, WALL_THICKNESS, chalk, -.575, WALL_CENTER_Y, -1.7);
      box(group, .27, WALL_HEIGHT, WALL_THICKNESS, chalk, 1.465, WALL_CENTER_Y, -1.7);
      box(group, ROOM_WIDTH, .51, WALL_THICKNESS, chalk, 0, 2.615, -1.7);
      for (const x of [-1.47, 1.47]) box(group, .26, WALL_HEIGHT, WALL_THICKNESS, chalk, x, WALL_CENTER_Y, 1.7);
      box(group, 2.68, .24, WALL_THICKNESS, chalk, 0, .39, 1.7);
      box(group, 2.68, .645, WALL_THICKNESS, chalk, 0, 2.5475, 1.7);
      // Door frame: floor .27 -> lintel 2.36; the wall continues above it.
      for (const x of [.45, 1.35]) box(group, .075, 2.05, .14, oak, x, 1.295, -1.7);
      box(group, .975, .08, .14, oak, .9, 2.32, -1.7);
      const hinge = new THREE.Group(); hinge.position.set(.49, .27, -1.7); hinge.rotation.y = -.98; group.add(hinge);
      box(hinge, .79, 1.96, .045, oak, .395, .98, 0);
      box(hinge, .56, .065, .012, walnut, .42, 1.76, -.03);
      cylinder(hinge, .026, .1, brass, .69, .99, .045).rotation.x = Math.PI / 2;
      const number = label(group, id.slice(1), .9, 2.48, -1.81); number.rotation.y = Math.PI;
      const floorNumber = label(group, id.slice(1), -.7, .275, -1.42, .72, .25); floorNumber.rotation.x = -Math.PI / 2;
      const occupancyMaterial = new THREE.MeshStandardMaterial({ color: 0xa7aca6, roughness: .55 }); materials.add(occupancyMaterial);
      const occupancy = cylinder(group, .075, .025, occupancyMaterial, .25, 1.65, -1.8, .075, false); occupancy.rotation.x = Math.PI / 2;
      // Window opening: 2.68 m wide, y=.51..2.225. Trim spans the wall's
      // thickness; low frosted glazing and curtains retain a privacy hint.
      for (const x of [-1.34, 1.34]) box(group, .06, 1.715, .22, oak, x, 1.3675, 1.7);
      for (const y of [.51, 2.225]) box(group, 2.74, .06, .22, oak, 0, y, 1.7);
      box(group, 2.68, .68, .025, frost, 0, .85, 1.71);
      box(group, 2.68, 1.03, .018, glass, 0, 1.71, 1.71);
      for (const side of [-1, 1]) {
        box(group, .17, 1.82, .08, linen, side * 1.22, 1.3, 1.6);
        box(group, .035, 1.82, .09, plaster, side * 1.2, 1.3, 1.59);
      }
      box(group, .025, .9, .62, frost, -1.39, .92, -1.08);
      // Bed, layered linen, book desk and reading chair.
      box(group, 1.42, .27, 1.91, oak, -.55, .42, -.15);
      box(group, 1.37, .19, 1.85, linen, -.55, .65, -.15);
      box(group, 1.4, .035, .68, material(level % 2 ? 0x90a095 : 0x9eb2ba), -.55, .765, .43);
      box(group, 1.48, .65, .09, oak, -.55, .65, -1.13);
      for (const x of [-.87, -.23]) {
        const pillow = orb(group, .24, linen, x, .8, -.76); pillow.scale.set(1.18, .25, .7);
      }
      box(group, .37, .47, .4, walnut, .45, .51, -.9);
      cylinder(group, .095, .035, brass, .45, .77, -.9);
      cylinder(group, .025, .18, brass, .45, .87, -.9);
      cylinder(group, .13, .16, material(0xe7d2a4), .45, 1.02, -.9, .09);
      box(group, 1.05, .018, 1.08, material(0xdcd4c1), .83, .275, .67);
      box(group, .67, .065, .55, oak, 1.02, .98, .4);
      for (const x of [.77, 1.27]) box(group, .045, .68, .045, dark, x, .62, .4);
      box(group, .18, .05, .23, material(0x6e8c81), .88, 1.04, .38);
      box(group, .19, .035, .23, material(0xd0a37c), .9, 1.08, .4);
      cylinder(group, .055, .09, linen, 1.2, 1.06, .4);
      box(group, .43, .1, .43, green, .94, .62, 1.08);
      box(group, .43, .45, .07, green, .94, .88, 1.28);
      for (const x of [.78, 1.1]) box(group, .035, .31, .035, dark, x, .42, 1.09);
      cylinder(group, .14, .25, plaster, -1.13, .41, 1.3, .19);
      const plant = orb(group, .21, green, -1.13, .75, 1.3); plant.scale.set(.8, 1.3, .8);
      box(group, .48, .52, .24, material(seed % 2 ? 0x8b8578 : 0xb6a28a), -.88, .53, -1.47);
      box(group, .2, .04, .08, dark, -.88, .83, -1.47);
      const figure = resident(group, seed);
      roomsById.set(id, { group, tile, occupancy, figure, floor: level + 1 });
    }
    bakeArchitecture(floor);
  }

  // Shared circulation stays open: each room doorway leads onto its wing
  // corridor, then into the central spine and the reception lobby.
  const shared = new THREE.Group(); shared.userData.illustrative = true; building.add(shared);
  const stone = material(0xd8d6c8), carpet = material(0xb7b7a2), terracotta = material(0xbc886c);
  box(shared, 3.7, .2, 28.4, chalk, 0, .12, 2.3);
  box(shared, 3.2, .027, 28.3, stone, 0, .235, 2.3);
  box(shared, 36.4, .2, 3.4, chalk, 0, .12, 0);
  box(shared, 36.4, .027, 2.95, stone, 0, .235, 0);
  for (const x of [-1.35, 1.35]) box(shared, .045, .03, 28.1, brass, x, .255, 2.3);
  for (const z of [-1.2, 1.2]) box(shared, 36.2, .03, .045, brass, 0, .255, z);
  box(shared, 36.45, .2, 6.3, chalk, 0, .12, 14.35);
  box(shared, 36.15, .028, 6.15, stone, 0, .235, 14.35);
  box(shared, 8.8, .03, 3.1, carpet, -10.9, .26, 14.5);

  function plant(parent, x, z, size = 1) {
    cylinder(parent, .26 * size, .5 * size, plaster, x, FLOOR_Y + .25 * size, z, .31 * size);
    const foliage = orb(parent, .43 * size, green, x, FLOOR_Y + .85 * size, z); foliage.scale.set(.82, 1.3, .82);
    cylinder(parent, .025 * size, .6 * size, oak, x, FLOOR_Y + .62 * size, z);
  }
  function sofa(parent, x, z, rotation = 0, mat = green) {
    const value = new THREE.Group(); value.position.set(x, FLOOR_Y, z); value.rotation.y = rotation; parent.add(value);
    box(value, 2.5, .34, .8, mat, 0, .3, 0);
    box(value, 2.5, .64, .18, mat, 0, .65, -.36);
    for (const side of [-1, 1]) box(value, .18, .52, .9, mat, side * 1.2, .48, 0);
    for (const cx of [-.78, 0, .78]) box(value, .72, .15, .68, linen, cx, .53, .02);
    for (const cx of [-.94, .94]) for (const cz of [-.25, .25]) cylinder(value, .035, .2, brass, cx, .1, cz);
    return value;
  }
  const reception = new THREE.Group(); reception.position.set(-3, 0, 14.1); reception.userData.zone = 'reception'; reception.userData.illustrative = true; shared.add(reception); zones.set('reception', reception);
  zoneLabel(reception, 'RECEPTION', 5);
  // The desk is empty on purpose: source data does not locate reception staff.
  box(reception, 4.9, .93, .95, dark, 0, .735, 0);
  box(reception, 5.15, .09, 1.15, oak, 0, 1.245, 0);
  box(reception, 4.9, .06, .035, brass, 0, .65, .49);
  for (let x = -2.3; x <= 2.3; x += .23) box(reception, .035, .67, .035, oak, x, .78, .495);
  for (const x of [-1.35, 1.15]) {
    box(reception, .36, .035, .22, dark, x, 1.315, -.05);
    box(reception, .055, .3, .055, dark, x, 1.46, -.1);
    box(reception, .7, .43, .06, dark, x, 1.69, -.1);
    box(reception, .61, .34, .008, material(0x9cb7b5), x, 1.69, -.137);
    box(reception, .55, .025, .19, walnut, x, 1.315, -.32);
  }
  cylinder(reception, .065, .055, brass, .15, 1.345, .26);
  const deskSign = label(reception, 'PLATO 40', 0, .94, .515, 1.45, .28);
  const lobbySign = label(shared, 'RECEPTION', -3, .28, 16.2, 3.3, .48); lobbySign.rotation.x = -Math.PI / 2;
  const planSign = label(shared, 'ILLUSTRATIVE LAYOUT', -3, .28, 17.1, 3.6, .34); planSign.rotation.x = -Math.PI / 2;
  sofa(shared, -11.7, 13, 0, terracotta); sofa(shared, -11.7, 16, Math.PI, green);
  cylinder(shared, .7, .065, oak, -11.7, .79, 14.5);
  cylinder(shared, .11, .49, dark, -11.7, .51, 14.5);
  box(shared, .32, .04, .24, linen, -11.8, .845, 14.52);
  cylinder(shared, .055, .08, chalk, -11.35, .86, 14.5);
  plant(shared, -15.8, 12.1, 1.1); plant(shared, -15.8, 16.6); plant(shared, 5.5, 16.5, 1.1);
  for (const [x, z] of [[-6.2, 12.1], [5.5, 12.1]]) {
    box(shared, 1.1, .13, .65, walnut, x, .45, z);
    for (const dx of [-.4, .4]) box(shared, .07, .22, .48, dark, x + dx, .32, z);
    box(shared, .46, .65, .27, terracotta, x - .18, .83, z);
    box(shared, .15, .04, .08, brass, x - .18, 1.18, z);
  }

  const housekeeping = new THREE.Group(); housekeeping.position.set(13, 0, 14.4); housekeeping.userData.zone = 'housekeeping'; housekeeping.userData.illustrative = true; shared.add(housekeeping); zones.set('housekeeping', housekeeping);
  zoneLabel(housekeeping, 'HOUSEKEEPING', 5.7);
  box(housekeeping, 8.1, .035, 5.5, material(0xc8d4cc), 0, .263, 0);
  for (const x of [-4, 4]) box(housekeeping, WALL_THICKNESS, WALL_HEIGHT, 5.6, chalk, x, WALL_CENTER_Y, 0);
  box(housekeeping, 8.1, WALL_HEIGHT, WALL_THICKNESS, chalk, 0, WALL_CENTER_Y, -2.7);
  box(housekeeping, 6, WALL_HEIGHT, WALL_THICKNESS, chalk, -1.1, WALL_CENTER_Y, 2.7);
  box(housekeeping, .9, WALL_HEIGHT, WALL_THICKNESS, chalk, 3.6, WALL_CENTER_Y, 2.7);
  box(housekeeping, 8.1, .51, WALL_THICKNESS, chalk, 0, 2.615, 2.7);
  for (const x of [1.9, 3.1]) box(housekeeping, .085, 2.05, .14, oak, x, 1.295, 2.7);
  box(housekeeping, 1.29, .08, .14, oak, 2.5, 2.32, 2.7);
  const utilityDoor = new THREE.Group(); utilityDoor.position.set(3.05, .27, 2.7); utilityDoor.rotation.y = -.9; housekeeping.add(utilityDoor);
  box(utilityDoor, 1.1, 1.96, .05, oak, -.55, .98, 0);
  const utilitySign = label(housekeeping, 'HOUSEKEEPING', -.9, .29, 1.9, 3.4, .42); utilitySign.rotation.x = -Math.PI / 2;
  // Open roof keeps the linen shelves and supplies visible from above.
  for (const x of [-2.85, -.85, 1.15]) {
    box(housekeeping, 1.7, .9, .57, oak, x, .72, -2.21);
    for (const side of [-1, 1]) box(housekeeping, .055, 1.45, .57, walnut, x + side * .8, 1, -2.21);
    for (const y of [.87, 1.25, 1.65]) {
      box(housekeeping, 1.65, .055, .57, oak, x, y, -2.21);
      for (const dx of [-.43, .23]) for (let stack = 0; stack < 3; stack++) box(housekeeping, .47, .065, .4, linen, x + dx, y + .065 + stack * .07, -2.19);
    }
  }
  for (const x of [-2.85, -1.6]) {
    box(housekeeping, 1.04, 1.07, .9, linen, x, .805, -.65);
    cylinder(housekeeping, .32, .045, dark, x, .8, -.17).rotation.x = Math.PI / 2;
    cylinder(housekeeping, .235, .05, frost, x, .8, -.145).rotation.x = Math.PI / 2;
    box(housekeeping, .57, .08, .025, material(0x9cb7b5), x, 1.2, -.18);
  }
  for (const x of [.4, 2.4]) {
    const cart = new THREE.Group(); cart.position.set(x, FLOOR_Y, .25); housekeeping.add(cart);
    for (const y of [.2, .75]) box(cart, .9, .065, .67, walnut, 0, y, 0);
    for (const dx of [-.4, .4]) for (const dz of [-.28, .28]) {
      cylinder(cart, .025, .85, brass, dx, .5, dz);
      cylinder(cart, .08, .06, dark, dx, .08, dz).rotation.z = Math.PI / 2;
    }
    for (let stack = 0; stack < 4; stack++) box(cart, .62, .07, .45, linen, 0, .28 + stack * .075, 0);
    for (const dx of [-.25, 0, .25]) cylinder(cart, .055, .19, material(dx ? 0x89a8a3 : 0xc7ae77), dx, .88, 0, .045);
  }
  cylinder(housekeeping, .24, .42, green, 3.35, .49, -1.3, .28);
  const mop = cylinder(housekeeping, .025, 1.7, oak, 3.4, 1.1, -1.25); mop.rotation.z = -.18;
  box(housekeeping, .4, .07, .19, linen, 3.55, .32, -1.25);
  bakeArchitecture(shared);

  const selection = new THREE.Group(); scene.add(selection); selection.visible = false;
  const selectionMaterial = new THREE.MeshBasicMaterial({ color: 0x24775b, transparent: true, opacity: .95 }); materials.add(selectionMaterial);
  for (const x of [-1.52, 1.52]) box(selection, .055, .035, 3.48, selectionMaterial, x, 0, 0, false);
  for (const z of [-1.74, 1.74]) box(selection, 3.1, .035, .055, selectionMaterial, 0, 0, z, false);

  let selected = 'R208', peopleVisible = true, currentRooms = [], animation, lastFrame = 0, down = null, cameraMode = 'overview';
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  let playback = { timeMs: 0, sourceTimeMs: null, playing: true, speed: 1, historical: false, anchoredAt: performance.now() };
  function illustrationTime(now) {
    const elapsed = playback.playing && !reducedMotion ? Math.max(0, now - playback.anchoredAt) * Math.min(8, playback.speed) : 0;
    return (playback.timeMs % DAY_MS + elapsed) / 1000;
  }
  function pose(person, time) {
    const { root, rig, torso, head, arms, legs, book, seed, activity } = person;
    const phase = seed * 1.73, idle = Math.sin(time * .8 + phase), walk = Math.sin(time * 4.6 + phase);
    root.rotation.set(0, 0, 0); rig.rotation.set(0, 0, 0); torso.scale.y = 1;
    head.rotation.set(0, idle * .09, 0); book.visible = activity === 1;
    for (const [index, arm] of arms.entries()) { arm.upper.rotation.set(0, 0, index ? -.08 : .08); arm.lower.rotation.x = -.12; }
    legs.forEach(leg => { leg.upper.rotation.x = 0; leg.lower.rotation.x = 0; });
    if (activity === 0) {
      // Resting on the mattress, not wandering outside the recorded room.
      root.position.set(-.55, .86, .52); root.rotation.x = -Math.PI / 2; root.scale.setScalar(.93);
      torso.scale.y = 1 + idle * .012; head.rotation.y = .12; arms.forEach(arm => { arm.upper.rotation.x = -.18; });
    } else if (activity === 1) {
      root.position.set(.94, .14, 1.09); root.rotation.y = Math.PI; root.scale.setScalar(.92);
      head.rotation.x = .15 + idle * .025;
      arms.forEach(arm => { arm.upper.rotation.x = -.65; arm.lower.rotation.x = -.95; });
      legs.forEach(leg => { leg.upper.rotation.x = -Math.PI / 2; leg.lower.rotation.x = Math.PI / 2; });
    } else if (activity === 2) {
      root.position.set(.3, FLOOR_Y, 1.29); root.scale.setScalar(.95);
      head.rotation.y = Math.sin(time * .32 + phase) * .22;
      arms[1].upper.rotation.x = -.62; arms[1].lower.rotation.x = -.5;
    } else {
      const route = Math.sin(time * .42 + phase);
      root.position.set(.87, FLOOR_Y + Math.abs(walk) * .012, -.35 + route * .4); root.scale.setScalar(.94);
      root.rotation.y = Math.cos(time * .42 + phase) >= 0 ? 0 : Math.PI;
      legs.forEach((leg, index) => { leg.upper.rotation.x = walk * (index ? -.3 : .3); leg.lower.rotation.x = Math.max(0, -walk * (index ? -1 : 1)) * .2; });
      arms.forEach((arm, index) => { arm.upper.rotation.x = walk * (index ? .22 : -.22); });
    }
  }
  function refresh() {
    const states = new Map(currentRooms.map(room => [room.id, room]));
    for (const [id, value] of roomsById) {
      const room = states.get(id), occupied = room?.occupancy === 'occupied';
      value.figure.occupied = occupied;
      value.figure.root.visible = peopleVisible && occupied;
      value.tile.material.color.setHex(PALETTE[room?.active_request_count ? 'attention' : room?.status] || PALETTE.unknown);
      value.tile.material.emissive.setHex(id === selected ? 0x285b43 : 0);
      value.tile.material.emissiveIntensity = id === selected ? .15 : 0;
      value.occupancy.material.color.setHex(occupied ? 0x356c59 : room?.occupancy === 'vacant' ? 0xe4ecdc : 0xa7aca6);
      value.occupancy.material.emissive.setHex(occupied ? 0x163e2c : 0);
      value.occupancy.material.emissiveIntensity = .2;
    }
    const value = roomsById.get(selected);
    if (value) {
      value.group.getWorldPosition(selection.position); selection.position.y += .29;
      selection.visible = true;
    } else selection.visible = false;
  }
  const raycaster = new THREE.Raycaster(), pointer = new THREE.Vector2();
  function point(event) {
    const rect = renderer.domElement.getBoundingClientRect(); if (!rect.width || !rect.height) return null;
    pointer.set((event.clientX - rect.left) / rect.width * 2 - 1, -(event.clientY - rect.top) / rect.height * 2 + 1);
    raycaster.setFromCamera(pointer, camera);
    return raycaster.intersectObjects(hitTargets).find(hit => hit.object.parent.visible && hit.object.parent.parent.visible);
  }
  const pointerDown = event => { down = [event.clientX, event.clientY]; };
  const pointerUp = event => {
    const origin = down; down = null;
    if (!origin || Math.hypot(event.clientX - origin[0], event.clientY - origin[1]) > 5) return;
    const hit = point(event); if (hit) onSelect(hit.object.userData.room);
  };
  const pointerMove = event => { renderer.domElement.style.cursor = point(event) ? 'pointer' : 'grab'; };
  const pointerCancel = () => { down = null; };
  renderer.domElement.addEventListener('pointerdown', pointerDown);
  renderer.domElement.addEventListener('pointerup', pointerUp);
  renderer.domElement.addEventListener('pointermove', pointerMove);
  renderer.domElement.addEventListener('pointercancel', pointerCancel);
  const overviewTarget = new THREE.Vector3(0, .75, 2.8), overviewDirection = new THREE.Vector3(.22, 2.5, 1.05).normalize();
  function overview() {
    cameraMode = 'overview';
    zoneLabels.forEach(value => { value.visible = true; });
    // Fit the actual one-level property in both axes. A fixed camera distance
    // clips wings on narrow displays and wastes space on the wide dashboard.
    const right = new THREE.Vector3().crossVectors(new THREE.Vector3(0, 1, 0), overviewDirection).normalize();
    const up = new THREE.Vector3().crossVectors(overviewDirection, right).normalize();
    const tanY = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2)), tanX = tanY * camera.aspect;
    let distance = 0;
    for (const x of [-22.5, 22.5]) for (const y of [-.35, 3.5]) for (const z of [-12.8, 19.2]) {
      const delta = new THREE.Vector3(x, y, z).sub(overviewTarget);
      distance = Math.max(distance, delta.dot(overviewDirection) + Math.abs(delta.dot(right)) / tanX,
        delta.dot(overviewDirection) + Math.abs(delta.dot(up)) / tanY);
    }
    distance *= 1.045;
    controls.maxDistance = Math.max(170, distance * 1.2); camera.far = Math.max(320, distance + 90); camera.updateProjectionMatrix();
    controls.target.copy(overviewTarget); camera.position.copy(overviewTarget).addScaledVector(overviewDirection, distance); controls.update();
  }
  function focus(group, distance) {
    if (!group) return false;
    cameraMode = 'detail';
    zoneLabels.forEach(value => { value.visible = false; });
    const direction = camera.position.clone().sub(controls.target).normalize();
    group.getWorldPosition(controls.target); controls.target.y = .85;
    camera.position.copy(controls.target).addScaledVector(direction, distance); controls.update(); return true;
  }
  const manualView = () => { cameraMode = 'manual'; zoneLabels.forEach(value => { value.visible = false; }); };
  controls.addEventListener('start', manualView);
  function resize() {
    const w = container.clientWidth, h = container.clientHeight; if (!w || !h) return;
    renderer.setSize(w, h); camera.aspect = w / h; camera.updateProjectionMatrix();
    if (cameraMode === 'overview') overview();
  }
  const observer = new ResizeObserver(resize); observer.observe(container); resize();
  function draw(now) {
    animation = requestAnimationFrame(draw);
    if (document.hidden || now - lastFrame < 26) return;
    lastFrame = now; const time = illustrationTime(now);
    for (const person of residents) if (person.root.visible && person.root.parent.parent.visible) pose(person, time);
    selectionMaterial.opacity = playback.historical && !playback.playing || reducedMotion ? .9 : .8 + Math.sin(time * 1.7) * .12;
    controls.update(); renderer.render(scene, camera);
  }
  refresh(); animation = requestAnimationFrame(draw);
  return {
    update(rooms, id) { currentRooms = Array.isArray(rooms) ? rooms : []; if (id) selected = id; refresh(); },
    // timeMs anchors a deterministic pose on a seek/restart, not every clock
    // update. Normal accelerated replay advances a separate 8x-bounded visual
    // clock; resident motion is never claimed to be a measured source stream.
    setPlayback({ timeMs, playing = true, speed = 1, historical = false, reason } = {}) {
      const now = performance.now(), current = illustrationTime(now) * 1000;
      const sourceTime = Number.isFinite(timeMs) ? timeMs : playback.sourceTimeMs;
      const reset = historical !== playback.historical || ['seek', 'restart', 'step', 'load'].includes(reason)
        || (!playback.playing && sourceTime !== playback.sourceTimeMs);
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
