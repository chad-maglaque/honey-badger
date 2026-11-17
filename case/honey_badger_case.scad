// Honey Badger — parametric enclosure, v2 (OpenSCAD 2021.01+; renders fastest with --backend=manifold)
//
// Holds, at real part sizes:
//   • Raspberry Pi 5 on a Geekworm X1202 4×18650 UPS board (98 × 86 mm, cells underneath),
//     2280 NVMe HAT on top — one stack on a PETG sled floating on 4 TPU dampers,
//     pressed down by lid posts through foam
//   • ALFA AWUS036AXML Wi-Fi 6 adapter (85 × 63 × 20 mm) standing on edge against the right wall
//   • radio bay: RTL-SDR, compact USB hub, RAK WisBlock Meshtastic node
//   • power-electronics tray (step-down, ideal diode, PD trigger, current sensor, fuse)
//   • front: recessed 1.3" OLED; low-profile knob, 12 mm button and status LED in one row in a recessed pocket;
//     2 × USB-A stacked in a second pocket
//   • rear: USB-C PD in, RJ45 (flush); GX16 solar/DC in in a well; SDR whip folds into a recessed channel
//     (the SDR is the only external antenna; set lora_ext = true for an optional capped mast jack)
//   • built-in Wi-Fi/LoRa flex antennas and GPS cradle under the lid
//   • 40 mm intake fan (left), 4 rain-shedding exhaust louvers (right), TPU lid gasket, 8 M3 lid screws
//   • TPU armour on every outside edge: a FRONT and a REAR FRAME (one part, printed twice) wrap the 4 edges of each face and
//     16 mm onto the top, bottom and sides; FOUR EDGE BUMPERS (one part, printed 4×) cover the long front-to-rear edges.
//     The edge bumpers slide into dovetail grooves in the side walls first; their thin end tenons tuck under the frames,
//     which then screw on (6 screws each, heads sunk in counterbores) and capture them.
//   Nothing on the front or rear stands out more than the bumpers' 6 mm, and every recess has 45° roofs and gussets so it prints without supports.
//
// Export one part at a time:   openscad --backend=manifold -D 'part="base"' -o base.stl honey_badger_case.scad
//   parts: base · lid · sled · damper · frame (×2) · edge_bumper (×4) · gasket · sdr_clip · sdr_shelf (optional 2nd SDR) · panel_test
//   views: assembly (fit check + clearance report) · showcase · exploded · frame_detail · edge_section · lid_inside · front_view · rear_view · lid_view
//
// Print: PETG (yellow body, black lid), TPU 95A (black) for bumpers, dampers, gasket. See docs/HARDWARE.md.
// MEASURE YOUR PARTS: every cutout and envelope below is a parameter. Dimensions in mm.
// Coordinates: x left→right, y front→back, z up, origin at the outer front-left-bottom corner.

part = "assembly";

/* [Markings] */
markings = "none";           // "none" (plain: add printed labels) · "inlay" (multi-colour print: pockets + part lid_inlay) · "engrave"

/* [Box] */
inner = [196, 160, 76];       // interior W × D × H (H = floor top to lid underside)
wall = 4.0;
floor_t = 3.0;
lid_t = 3.5;
corner_r = 7;
edge_fillet = 2;              // rounded bottom edge of the base and top edge of the lid
seam_fillet = 0.8;            // rounded top edge of the base and bottom edge of the lid (they meet in a soft V)
rim_fillet = 1.5;             // rounded rims on the control pocket, whip channel and wells
lip_h = 4;                    // lid locating lip that drops inside the walls
gasket_w = 2.2;               // groove in the lid
gasket_d = 1.6;
gasket_print = [1.5, 2.1];    // printed TPU cord: 89 % groove fill, ~24 % squeeze
clear = 0.25;
logo_mm = 38;
logo_depth = 0.8;

/* [Hardware] */
insert_d = 4.2;               // M3 heat-set insert (5.7 mm long)
insert_depth = 6;
m3_clear = 3.4;
m3_head_d = 6.2;
m25_insert_d = 3.6;
boss_d = 9;

/* [Stack: UPS + Pi + NVMe] */
ups_size = [98, 86];          // Geekworm X1202 board footprint (cells hang underneath)
ups_at = [30, 44];            // UPS board front-left corner (box coordinates)
pi_in_ups = [6, 29];          // where the Pi sits on the UPS board (Pi holes = UPS holes)
pi_board = [85, 56];
pi_holes = [58, 49];
pi_hole_inset = 3.5;
port_plug_room = 25;          // free space beyond the Pi's USB/Ethernet edge for plugs (use 90° adapters)
ups_h = 24;                   // UPS board + cells envelope
pi_h = 19;                    // Pi 5 + Active Cooler
nvme_h = 9;                   // NVMe HAT + standoffs
pi_at = ups_at + pi_in_ups;   // Pi corner (box coordinates)

/* [Radio bay] (box coordinates) */
wifi_box = [20, 85, 63];      // AWUS036AXML standing on its edge: thickness × length × height
wifi_at = [176.6, 64];        // inner face of the slot (x) and front end (y)
sdr_box = [25, 70, 14];       // RTL-SDR dongle lying flat, along y
sdr_at = [136, 68];
hub_box = [44, 25, 12];       // compact 4-port USB 2.0 hub board (≤ 44 × 25 mm)
hub_at = [130.5, 42];
rak_box = [60, 30, 14];       // RAK19007 + RAK4631 (Meshtastic), in the front-right bay
rak_at = [118, 12];

/* [Front panel] (x from left outer edge, z from floor top) */
// One recessed band between the bumpers holds everything, centred on one line and spaced evenly (8 mm gaps):
// screen (36) · knob (14) · button (16) · light (8) · USB stack (34). Top and bottom slope 45°, so it prints
// without supports, looks the same above and below the line, and sheds rain.
front_cl = 39;                // the centre line every front part sits on
front_band = [24, 180, 20, 58]; // x0, x1, z0, z1 at the band's back face (the face opening is 8 mm bigger above and below)
front_depth = 8;              // low-profile knob ends 2.4 mm proud of the face, inside the 6 mm bumpers
oled_window = [31, 15];
oled_module = [36, 23.6];     // pocket for the module glass, 0.6 mm clearance
oled_at = [50, front_cl];
oled_holes = [30.4, 28.5];
oled_screw_d = 2.4;
encoder_d = 7.4;
encoder_tab = [0, 7.5, 2.4];  // anti-rotation tab: x, y offset and hole d (check your EC11)
encoder_at = [83, front_cl];
button_d = 12.2;
button_at = [106, front_cl];
led_d = 6.3;                  // 5 mm LED in a clip-in holder
led_at = [126, front_cl];
usba_cut = [13.6, 6.4];
usba_screw = 28;
usba_at = [[155, front_cl + 10], [155, front_cl - 10]];   // stacked: USB 1 above USB 2
front_well_t = 2.5;           // band back = mounting panel for the screen, EC11, button, LED and USB ports

/* [Rear panel] (x measured from the left edge AS SEEN FROM BEHIND, z from floor top) */
// One recessed band across the rear, the same width as the front band and 12 mm deep: deep enough to hide the GX16 socket
// and the folded SDR whip. Ethernet, USB-C and GX16 share one line, evenly spaced; the whip folds on a line above them.
rear_band = [24, 180, 13, 51]; // rear-view x0, x1, z0, z1 at its back face (roof rises 45° to the face; the floor drops 2 mm to drain)
rear_depth = 12;
rear_cl = 24;
usbc_cut = [9.6, 3.8];
usbc_screw = 20.5;
usbc_at = [106.5, rear_cl];
gx16_d = 16.2;
gx16_at = [149.25, rear_cl];
rj45_cut = [16.5, 14];
rj45_screw = 30;
rj45_at = [59.25, rear_cl];
sma_d = 6.6;
sma_at = [[44, 43, "SDR"], [44, 28, "LoRa EXT"]];
// the folded whip lies inside the band; it swings up out of it when tilted back about 20°
gx16_room = 12;               // finger room around the GX16 axis for the plug's lock ring
lora_ext = false;             // optional LoRa mast jack (the LoRa antenna is built in under the lid)
sma_well = [14, 9];           // LoRa EXT well: capped jack ends 2 mm proud
rear_well_t = 3;

/* [Panel thickness reliefs] [counterbore d, panel left] */
encoder_relief = [16, 2.0];
button_relief = [16, 3.0];
led_relief = [10, 2.5];
sma_relief = [12, 2.0];
screen_chamfer = 1.5;

/* [SDR whip stowage] */
sdr_whip_d = 9;
sdr_whip_offset = 13;         // whip axis from the rear face when folded
sdr_whip_len = 125;           // folded length from the knuckle (measure yours)
sdr_clip_x = [85, 125];       // rear-view x of the two clips
clip_screw_dx = 13;           // clip screws sit beside the ring, ±13 mm

/* [Second SDR (standard: aircraft + weather satellites)] */
// "jack": the SDR 2 bulkhead jack under the SDR whip jack (standard);
// "knockout": printed closed by a thin inner skin, drilled out (7 mm) later; "none": no jack.
sdr2 = "jack";
sdr2_jack = [85.25, 22];      // rear view x, z: midway between the Ethernet and USB-C ear screws, under the whip clip with finger room
rj45_body_depth = 22;         // panel-mount RJ45 jack body behind the band (Adafruit-style extension)
sdr2_pigtail = 8;             // right-angle SMA plug on the second dongle, turned toward the Wi-Fi side
sdr2_box = [25, 70, 14];      // second RTL-SDR, lying flat on a shelf above the first
sdr2_at = [136, 50];          // overhangs the 12 mm hub; stops short of the RJ45 body at the back
shelf_t = 2.5;
shelf_gap = 1.5;              // air above the first dongle
sdr2_z = sdr_box.z + shelf_gap + shelf_t;   // second dongle's underside above the floor top
shelf_posts = [[166.5, 84], [166.5, 112]];  // M3 bosses right of the first dongle (clear of its zip-tie anchor at y 98)
shelf_post_d = 7;
shelf_boss_h = 5;
shelf_leg = [133.2, 88, 2.6, 24];           // x, y, thickness, length: the left leg stands in the gap between the sled and the first dongle
shelf_cbore = 6;              // M3×12 from the top, head 6 mm down in the post
knockout_skin = 0.6;

/* [Sensor socket] */
// M8 4-pin (A-coded, IP67) panel socket for the sensor station: 1 = 3.3 V (polyfused), 2 = GND, 3 = 1-Wire, 4 = float switch.
// Deliberately a different connector from the GX16 power input, so the two can never be swapped.
sensor_sock = true;
sensor_sock_d = 8.2;          // M8 thread
sensor_sock_nut = 11.6;       // nut / flange diameter on the outside
sensor_sock_at = [128.6, rear_cl];   // rear view: midway between the USB-C ear screw and the GX16 nut

/* [Internal antennas] (lid coordinates = box x, y) */
wifi_fpc = [48, 12];
wifi_a_at = [16, 14];         // front-left, along x (over the screen)
wifi_b_at = [180, 80];        // over the Wi-Fi adapter, along y
lora_fpc = [20, 80];
lora_at = [150, 62];
gps_box = [42, 42, 16];
gps_at = [70, 12];            // over the power tray, where there's headroom above the stack and the panel parts
lid_anchors = [[120, 30], [176, 140]];

/* [Power electronics tray] */
power_tray = [80, 26];
power_tray_at = [34, 14];
tray_boss = [8.5, 7];         // boss diameter, height above the floor top

/* [Airflow] */
fan = 40;
fan_hole_spacing = 32;
fan_at = [118, 40];           // y, z on the left wall
louver_y = [19.5, 29, 38.5, 56.5];   // right wall: front edge of each slot. Three between the front frame (y ≤ 16) and the side
                              // screw boss (y 45.5-54.5), one between the boss and the Wi-Fi slot (y ≥ 64)
louver_size = [5, 20];        // slot width (y) × height (z)
louver_z = 43.5;              // slot bottom on the outer face (box z): between the edge bumpers (z ≤ 16 and ≥ 66.6); slopes down 0.6:1 outward

/* [Ruggedization] */
sled_t = 4;
sled_size = [102, 90];
damper_d = 11.6;
damper_h = 7;
damper_peg_d = 4.6;
damper_inset = [8, 16];       // keeps the pegs clear of the UPS screw inserts
cup_h = 5;
foam_t = 3;
post_d = 8;
post_preload = 1.0;           // posts press 1 mm into the foam
bumper_t = 6;                 // TPU thickness over every face: frames and edge bumpers stand 6 mm off the case all round
bumper_leg = 16;              // how far the front and rear frames wrap onto the top, bottom and sides
bumper_cbore = [7.4, 2.8];    // counterbore for an M3 button-head screw + washer: head ends 0.65 mm below the surface
bumper_fillet = 1.5;          // every outside edge of the bumpers is rounded
bumper_clear = 0.2;           // gap between the TPU and the case
edge_leg = 16;                // the edge bumpers wrap this far onto the top/bottom and the side, measured from the edge
edge_r = 2;                   // case edge radius the bumpers follow (lid top 1.7, base bottom 2)
tenon = [2.5, 8, 0.3];        // edge-bumper end tenon: plate thickness, starts this far from the face (y), clearance in the frame slot
dove = [3, 1.5, 9, 0.2];      // edge-bumper tongue / side-wall groove: mouth width, depth (45° flanks), centre from the top/bottom face, clearance
frame_open = [21, 183, 11.5, 71.5];   // frame window at the case face (box x0, x1, z0, z1): clears both bands' rims by ≥ 1 mm
frame_open_r = 5.5;
frame_chamfer = 4;            // 45° lead-in round the window (also lets the SDR whip stand up)
clip_top = bumper_t + rear_depth - sdr_whip_offset - 0.3;   // SDR clip ring crown above the whip axis: 0.3 mm inside the bumpers
frame_boss = 4.4;             // solid TPU round each corner screw where the tenon slot stops (the tenon is notched 0.3 more)

$fn = 48;
outer = [inner.x + 2 * wall, inner.y + 2 * wall, inner.z + floor_t];
sled_xy = ups_at - [2, 2];
damper_pts = [for (x = [damper_inset.x, sled_size.x - damper_inset.x], y = [damper_inset.y, sled_size.y - damper_inset.y]) sled_xy + [x, y]];
stack_z0 = floor_t + damper_h + sled_t;
stack_top = stack_z0 + ups_h + pi_h + nvme_h;
pi_hole_pts = [for (dx = [0, pi_holes.x], dy = [0, pi_holes.y]) pi_at + [pi_hole_inset + dx, pi_hole_inset + dy]];
Hc = outer.z + lid_t + 0.1;   // top of the lid (+0.1 slack): the height the bumpers wrap
lid_ft = min(edge_fillet, lid_t - 1 - seam_fillet);   // the lid's rounded top edge
corner_pts = [[corner_r - 1, corner_r - 1], [outer.x - corner_r + 1, corner_r - 1],
              [corner_r - 1, outer.y - corner_r + 1], [outer.x - corner_r + 1, outer.y - corner_r + 1]];
// lid screws: 4 corners + a mirror-symmetric set of 4 edge screws (front/rear on the centre line, left/right level at y 50,
// clear of the Wi-Fi slot). The front and rear ones pass above the front band and the whip channel.
screw_pts = concat(corner_pts, [[outer.x / 2, wall + 1.5], [outer.x / 2, outer.y - wall - 1.5],
                                [wall + 1.5, 50], [outer.x - wall - 1.5, 50]]);
frame_pts = [screw_pts[0], screw_pts[1], screw_pts[4]];   // front frame: 2 corner + middle lid screws on top, the same 3 underneath
                                                        // (the rear frame is the same part turned round: screw_pts 2, 3, 5)
rv = function(x) outer.x - x;  // rear-view x → box x

// ───────────────────────── helpers ─────────────────────────
module rbox_f(size, r, fb = 0, ft = 0) {
    hull() for (x = [r, size.x - r], y = [r, size.y - r]) translate([x, y, 0]) {
        if (fb > 0) translate([0, 0, fb]) rotate_extrude($fn = 48) translate([r - fb, 0]) circle(r = fb, $fn = 24);
        else cylinder(r = r, h = 0.01);
        translate([0, 0, fb]) cylinder(r = r, h = size.z - fb - ft);
        if (ft > 0) translate([0, 0, size.z - ft]) rotate_extrude($fn = 48) translate([r - ft, 0]) circle(r = ft, $fn = 24);
        else translate([0, 0, size.z - 0.01]) cylinder(r = r, h = 0.01);
    }
}
module rbox(size, r) { hull() for (x = [r, size.x - r], y = [r, size.y - r]) translate([x, y, 0]) cylinder(r = r, h = size.z); }
module rr2d(size, r) { hull() for (x = [r, size.x - r], y = [r, size.y - r]) translate([x, y]) circle(r = r); }

module on_front(x, z) { translate([x, -1, floor_t + z]) rotate([-90, 0, 0]) children(); }       // local z: 1 = outer face
module on_rear(x, z)  { translate([rv(x), outer.y + 1, floor_t + z]) rotate([90, 0, 0]) children(); }
module on_left(y, z)  { translate([-1, y, floor_t + z]) rotate([0, 90, 0]) children(); }
module on_right(y, z) { translate([outer.x + 1, y, floor_t + z]) rotate([0, -90, 0]) children(); }
module relief(r, t = wall) { if (t > r[1]) translate([0, 0, 1 + r[1]]) cylinder(d = r[0], h = t - r[1] + 0.01); }
// extrude a 2D (x, z) profile along y, between two y planes
module xz_extrude(y0, y1) { translate([0, max(y0, y1), 0]) rotate([90, 0, 0]) linear_extrude(abs(y1 - y0)) children(); }
module teardrop2d(d) { hull() { circle(d = d); translate([0, d / 2 * sqrt(2) - 0.01]) square(0.02, center = true); } }   // 45° point up
// A recess in the front (face 0) or rear (face 1) wall. Profile: [x0, x1, z0, z1] in box x / floor-relative z at the back face;
// the roof rises 45° to the outer face so it prints without support. mode 0 = solid backing (with a 45° gusset underneath), 1 = void.
function yf(face, d) = face == 0 ? d : outer.y - d;
module rect_well(face, p, depth, t, mode, sym = false, drop = 0, flat = false, r = 3) {
    // sym: top and bottom both slope 45° · drop: the floor falls this much toward the face (drains) · flat: straight walls
    // (only for pockets narrow enough that the roof prints as a short bridge)
    module prof(e, rise) let(up = flat ? 0 : rise, dn = sym ? up : (rise > 0 ? drop : 0))
        translate([p[0] - e, floor_t + p[2] - e - dn]) rr2d([p[1] - p[0] + 2 * e, p[3] - p[2] + up + dn + 2 * e], r + e);
    if (mode == 1) {
        hull() { xz_extrude(yf(face, -1), yf(face, 0.01)) prof(0, depth + 1); xz_extrude(yf(face, depth - 0.01), yf(face, depth)) prof(0, 0); }
        rim_fillet(face, rim_fillet) prof(0, depth);
    }
    else hull() {
        xz_extrude(yf(face, 0), yf(face, wall)) prof(t, depth);
        xz_extrude(yf(face, depth), yf(face, depth + t)) prof(t, 0);
        if (!sym) xz_extrude(yf(face, wall - 0.01), yf(face, wall)) translate([p[0] - t, floor_t + p[2] - t - (depth + t - wall)]) square([p[1] - p[0] + 2 * t, 1]);
    }
}
module round_well(face, x, z, d, depth, t, mode) {
    module prof(e) translate([x, floor_t + z]) teardrop2d(d + 2 * e);
    if (mode == 1) { xz_extrude(yf(face, -1), yf(face, depth)) prof(0); rim_fillet(face, rim_fillet) prof(0); }
    else hull() {
        xz_extrude(yf(face, 0), yf(face, depth + t)) prof(t);
        xz_extrude(yf(face, wall - 0.01), yf(face, wall)) translate([x - d / 2 - t, floor_t + z - d / 2 - t - (depth + t - wall)]) square([d + 2 * t, 1]);
    }
}
// quarter-circle steps for fillets: [depth from the surface, outward offset]
function fillet_steps(r, n = 6) = [for (i = [0:n]) let(a = 90 * i / n) [r * (1 - cos(a)), r * (1 - sin(a))]];
module rim_fillet(face, r, d0 = 0) {   // children(): the 2D opening profile; rounds its edge into the surface at depth d0
    st = fillet_steps(r);
    for (i = [0:len(st) - 2]) hull() {
        xz_extrude(yf(face, d0 + st[i][0] - (i == 0 ? 1 : 0)), yf(face, d0 + st[i][0] + 0.01)) offset(delta = st[i][1]) children();
        xz_extrude(yf(face, d0 + st[i + 1][0]), yf(face, d0 + st[i + 1][0] + 0.01)) offset(delta = st[i + 1][1]) children();
    }
}
rband_x = [rv(rear_band[1]), rv(rear_band[0])];   // rear band in box x
module wells(mode) {
    rect_well(0, front_band, front_depth, front_well_t, mode, sym = true);
    rect_well(1, [rband_x[0], rband_x[1], rear_band[2], rear_band[3]], rear_depth, rear_well_t, mode, drop = 2);
    if (lora_ext) round_well(1, rv(sma_at[1].x), sma_at[1].y, sma_well[0], sma_well[1], rear_well_t, mode);
}
module anchor_pair() { for (dx = [0, 6]) translate([dx, 0, 0]) difference() { cube([3, 6, 6]); translate([-1, 1.5, 2]) cube([5, 3, 2.4]); } }
module slot(size, depth = wall + 2) { translate([-size.x / 2, -size.y / 2, 0]) cube([size.x, size.y, depth]); }
module hole(d, depth = wall + 2) { cylinder(d = d, h = depth); }
module ears(spacing, d = m3_clear) { for (s = [-1, 1]) translate([s * spacing / 2, 0, 0]) hole(d); }

// ───────────────────────── base ─────────────────────────
module base() {
    difference() {
        union() {
            difference() {
                rbox_f(outer, corner_r, fb = edge_fillet, ft = seam_fillet);
                translate([wall, wall, floor_t]) rbox([inner.x, inner.y, inner.z + 1], max(1, corner_r - wall));
            }
            for (p = screw_pts) translate([p.x, p.y, 0]) cylinder(d = boss_d, h = outer.z);
            // damper cups (the sled floats on TPU pucks)
            for (d = damper_pts) translate([d.x, d.y, 0]) difference() {
                cylinder(d = damper_d + 4.4, h = floor_t + cup_h);
                translate([0, 0, floor_t]) cylinder(d = damper_d + 0.4, h = cup_h + 1);
            }
            // Wi-Fi adapter slot: stands on edge against the right wall
            translate([wifi_at.x - 1.6, wifi_at.y, floor_t]) difference() {
                cube([wifi_box.x + 0.6 + 3.2, wifi_box.y + 1.6, 12]);
                translate([1.6, 0.8, 1]) cube([wifi_box.x + 0.6, wifi_box.y, 12]);
                for (y = [20, wifi_box.y - 24]) translate([-1, y, 4]) cube([wifi_box.x + 6, 4, 2.4]);   // strap slots
            }
            // radio-bay anchors: SDR, hub, RAK node are zip-tied to these
            for (a = [[sdr_at.x - 6, sdr_at.y + 10], [sdr_at.x - 6, sdr_at.y + 50], [sdr_at.x + sdr_box.x + 1, sdr_at.y + 30],
                      [hub_at.x + hub_box.x + 1, hub_at.y + 8], [rak_at.x + rak_box.x + 1, rak_at.y + 8]])
                translate([a.x, a.y, floor_t]) anchor_pair();
            // second-SDR shelf bosses (always printed, so the kit can be added later)
            for (p = shelf_posts) translate([p.x, p.y, 0]) cylinder(d = shelf_post_d, h = floor_t + shelf_boss_h);
            // power tray bosses
            for (x = [4.25, power_tray.x - 4.25], y = [4.25, power_tray.y - 4.25])
                translate([power_tray_at.x + x, power_tray_at.y + y, 0]) cylinder(d = tray_boss[0], h = floor_t + tray_boss[1]);
            // left floor cable channel anchors (DC input → tray → UPS)
            for (y = [66, 96, 140]) translate([wall + 1, y, floor_t]) anchor_pair();
            // floor anchor for the antenna cables that tether the lid (rear right)
            translate([165, 140, floor_t]) anchor_pair();
            intersection() { wells(0); translate([wall / 2, wall / 2, 0]) cube([outer.x - wall, outer.y - wall, outer.z]); }   // backings, kept inside the shell
            // SDR clip screw bosses behind the whip channel, with 45° gussets so they print without support
            for (cx = sdr_clip_x, dx = [-clip_screw_dx, clip_screw_dx]) translate([rv(cx) + dx, outer.y - rear_depth - rear_well_t, floor_t + sma_at[0].y]) {
                rotate([90, 0, 0]) cylinder(d = 9, h = 7);
                translate([-4.5, -7, -11.5]) difference() { cube([9, 7, 7]); translate([-1, 0, 0]) rotate([45, 0, 0]) cube([11, 10, 10]); }
            }
        }
        // lid inserts (top of every boss) + frame bottom screws (bottom of the corner and front/rear middle bosses)
        for (p = screw_pts) translate([p.x, p.y, outer.z - insert_depth]) cylinder(d = insert_d, h = insert_depth + 1);
        for (i = [0:5]) translate([screw_pts[i].x, screw_pts[i].y, -1]) cylinder(d = insert_d, h = insert_depth + 1);
        // tray inserts
        for (x = [4.25, power_tray.x - 4.25], y = [4.25, power_tray.y - 4.25])
            translate([power_tray_at.x + x, power_tray_at.y + y, floor_t + tray_boss[1] - insert_depth]) cylinder(d = insert_d, h = insert_depth + 1);
        // second-SDR shelf inserts
        for (p = shelf_posts) translate([p.x, p.y, floor_t + shelf_boss_h - insert_depth]) cylinder(d = insert_d, h = insert_depth + 1);
        // SDR clip inserts (blind from outside into the boss)
        for (cx = sdr_clip_x, dx = [-clip_screw_dx, clip_screw_dx]) translate([rv(cx) + dx, outer.y - rear_depth + 0.01, floor_t + sma_at[0].y])
            rotate([90, 0, 0]) cylinder(d = insert_d, h = insert_depth);
        // dovetail grooves along the side walls for the edge bumpers' tongues (open at both ends, so the bumpers slide in)
        for (i = [0:3]) at_edge(i) w_extrude(-1, outer.y + 1) dove2d(0);
        wells(1);
        front_cutouts();
        rear_cutouts();
        left_cutouts();
        right_cutouts();
    }
}

module front_cutouts() {
    // everything mounts on the back of the front band (2.5 mm panel)
    translate([0, front_depth, 0]) {
        on_front(oled_at.x, oled_at.y) {
            slot(oled_window);
            for (sx = [-1, 1], sy = [-1, 1]) translate([sx * oled_holes.x / 2, sy * oled_holes.y / 2, 0]) hole(oled_screw_d);
        }
        on_front(encoder_at.x, encoder_at.y) {
            hole(encoder_d); relief(encoder_relief, front_well_t);
            translate([encoder_tab.x, encoder_tab.y, 1 + encoder_relief[1] - 1.3]) hole(encoder_tab[2], 1.4);   // blind, from the inside
        }
        on_front(button_at.x, button_at.y) { hole(button_d); relief(button_relief, front_well_t); }
        on_front(led_at.x, led_at.y) { hole(led_d); relief(led_relief, front_well_t); }
        for (u = usba_at) on_front(u.x, u.y) { slot(usba_cut); ears(usba_screw); }
    }
    rim_fillet(0, 0.8, front_depth) translate([oled_at.x, floor_t + oled_at.y]) square(oled_window, center = true);   // rounded window rim
}

module rear_cutouts() {
    // everything mounts on the back of the rear band (3 mm panel)
    translate([0, -rear_depth, 0]) {
        on_rear(usbc_at.x, usbc_at.y) { slot(usbc_cut); ears(usbc_screw, 2.7); }      // M2.5 ears
        on_rear(gx16_at.x, gx16_at.y) hole(gx16_d);
        on_rear(rj45_at.x, rj45_at.y) { slot(rj45_cut); ears(rj45_screw); }
        on_rear(sma_at[0].x, sma_at[0].y) { hole(sma_d); relief(sma_relief, rear_well_t); }
        // second SDR's jack, in the spare position under the first
        if (sdr2 == "jack") on_rear(sdr2_jack.x, sdr2_jack.y) { hole(sma_d); relief(sma_relief, rear_well_t); }
        else if (sdr2 == "knockout") on_rear(sdr2_jack.x, sdr2_jack.y) hole(sma_d, 1 + rear_well_t - knockout_skin);
        if (sensor_sock) on_rear(sensor_sock_at.x, sensor_sock_at.y) hole(sensor_sock_d);
    }
    if (lora_ext) translate([0, -sma_well[1], 0]) on_rear(sma_at[1].x, sma_at[1].y) { hole(sma_d); relief(sma_relief, rear_well_t); }
}

module left_cutouts() {
    on_left(fan_at.x, fan_at.y) {
        difference() {
            hole(fan - 2);
            for (r = [8, 15]) difference() { cylinder(r = r + 1, h = wall + 2); cylinder(r = r - 1, h = wall + 2); }
            for (a = [0:60:179]) rotate([0, 0, a]) translate([-1, -fan, 0]) cube([2, 2 * fan, wall + 2]);
        }
        for (sx = [-1, 1], sy = [-1, 1]) translate([sx * fan_hole_spacing / 2, sy * fan_hole_spacing / 2, 0]) hole(m3_clear);
    }
}

module right_cutouts() {
    // louvers slope DOWN toward the outside, so rain runs off instead of in; their round tops print as short bridges
    lw = louver_size[0]; lh = louver_size[1];
    module stadium() hull() for (z = [lw / 2, lh - lw / 2]) translate([lw / 2, z]) circle(d = lw);   // lw × lh slot in (y, z)
    module yz_slab(x0, x1) translate([x0, 0, 0]) rotate([90, 0, 90]) linear_extrude(x1 - x0) children();
    st = fillet_steps(1.0);
    for (y = louver_y) translate([outer.x - wall - 1, y, louver_z + 0.6 * (wall + 1)])
        multmatrix([[1, 0, 0, 0], [0, 1, 0, 0], [-0.6, 0, 1, 0]]) {
            yz_slab(0, wall + 2) stadium();
            for (i = [0:len(st) - 2]) hull() {
                yz_slab(wall + 1 - st[i][0] - 0.01, wall + 1 - st[i][0] + (i == 0 ? 1 : 0)) offset(delta = st[i][1]) stadium();
                yz_slab(wall + 1 - st[i + 1][0] - 0.01, wall + 1 - st[i + 1][0]) offset(delta = st[i + 1][1]) stadium();
            }
        }
}

// ───────────────────────── lid ─────────────────────────
module lid() {
    difference() {
        union() {
            rbox_f([outer.x, outer.y, lid_t], corner_r, fb = seam_fillet, ft = lid_ft);
            fpc_frame(wifi_a_at, wifi_fpc);
            fpc_frame(wifi_b_at, [wifi_fpc.y, wifi_fpc.x]);
            fpc_frame(lora_at, lora_fpc);
            translate([gps_at.x, gps_at.y, -6]) difference() {
                cube([gps_box.x + 4, gps_box.y + 4, 6]);
                translate([2, 2, -1]) cube([gps_box.x + 0.4, gps_box.y + 0.4, 8]);
                translate([-1, gps_box.y / 2 - 2, -1]) cube([gps_box.x + 6, 4, 4]);
            }
            for (a = lid_anchors) translate([a.x, a.y, 0]) mirror([0, 0, 1]) anchor_pair();
            // hold-down posts with 1 mm preload and a 45° foot that prints face-down without support
            for (x = [6, pi_board.x - 6], y = [6, pi_board.y - 6]) {
                post_len = outer.z - stack_top - foam_t + post_preload;
                translate([pi_at.x + x, pi_at.y + y, -post_len]) {
                    cylinder(d = post_d, h = post_len);
                    cylinder(d1 = post_d + 4, d2 = post_d, h = 2);
                }
            }
            // locating lip, notched around every screw boss
            difference() {
                translate([wall + clear, wall + clear, -lip_h]) difference() {
                    rbox([inner.x - 2 * clear, inner.y - 2 * clear, lip_h], max(1, corner_r - wall));
                    translate([2, 2, -1]) rbox([inner.x - 2 * clear - 4, inner.y - 2 * clear - 4, lip_h + 2], max(1, corner_r - wall - 2));
                }
                for (p = screw_pts) translate([p.x, p.y, -lip_h - 1]) cylinder(d = boss_d + 1, h = lip_h + 1);
            }
        }
        translate([0, 0, -0.01]) difference() {                       // gasket groove over the wall tops
            translate([wall / 2 - gasket_w / 2, wall / 2 - gasket_w / 2, 0])
                rbox([outer.x - wall + gasket_w, outer.y - wall + gasket_w, gasket_d], corner_r - wall / 2 + gasket_w / 2);
            translate([wall / 2 + gasket_w / 2, wall / 2 + gasket_w / 2, -1])
                rbox([outer.x - wall - gasket_w, outer.y - wall - gasket_w, gasket_d + 2], corner_r - wall / 2 - gasket_w / 2);
        }
        for (i = [0:len(screw_pts) - 1]) translate([screw_pts[i].x, screw_pts[i].y, -lip_h - 1]) {
            cylinder(d = m3_clear, h = lid_t + lip_h + 2);
            // the 2 side screws sit in counterbores (the top edge bumpers lie over them); the corner and front/rear middle
            // screws clamp the frames, so the lid stays flat there
            if (i >= 6) translate([0, 0, lip_h + 1 + lid_t - 2.2]) { cylinder(d = m3_head_d, h = 3); translate([0, 0, 2.2 - 0.4]) cylinder(d1 = m3_head_d, d2 = m3_head_d + 0.8, h = 0.41); }
        }
        if (markings != "none") translate([0, 0, lid_t - logo_depth]) linear_extrude(logo_depth + 1) lid_marks();
        translate([outer.x - 50, 18, lid_t - 0.5]) linear_extrude(1) rr2d([30, 30], 2);   // QR sticker recess, clear of the frame and edge bumper
    }
}

module fpc_frame(at, size) {
    translate([at.x - 1.2, at.y - 1.2, -1.2]) difference() {
        cube([size.x + 2.4, size.y + 2.4, 1.2]);
        translate([1.2, 1.2, -1]) cube([size.x, size.y, 3]);
        translate([size.x > size.y ? size.x : -1, size.x > size.y ? size.y / 2 - 1.5 : size.y, -1]) cube([4, 4, 3]);
    }
}

// The lid artwork (2D): badger mark, name and join hint. Used as engraving, multi-colour inlay, or the printed label in renders.
module lid_logo() translate([21, outer.y / 2 - 19]) badger_coat(logo_mm);   // clear of the left edge bumper (x ≤ 16)
module lid_text() {
    translate([outer.x / 2 + 18, outer.y / 2 + 6]) text("HONEY BADGER", size = 11, halign = "center", font = "Liberation Sans:style=Bold");
    translate([outer.x / 2 + 18, outer.y / 2 - 10]) text("Join Wi-Fi · open badger.lan", size = 5, halign = "center", font = "Liberation Sans");
}
module lid_marks() { lid_logo(); lid_text(); }
module lid_inlay() linear_extrude(logo_depth) offset(delta = -0.05) lid_marks();   // print in the lid's first layers, face down

// ───────────────────────── printed parts ─────────────────────────
module sled() {
    difference() {
        union() {
            rbox([sled_size.x, sled_size.y, sled_t], 4);
            for (d = damper_pts) translate(d - sled_xy) translate([0, 0, -(damper_h - 2)]) cylinder(d = damper_peg_d, h = damper_h - 2 + 0.01);
        }
        for (h = pi_hole_pts) translate(concat(h - sled_xy, [-1])) cylinder(d = m25_insert_d, h = sled_t + 2);
        for (x = [26, sled_size.x - 26], y = [-1, sled_size.y - 4]) translate([x - 10, y + 1.5, -1]) cube([20, 3, sled_t + 2]);   // cell strap
        translate([30, 24, -1]) rbox([sled_size.x - 60, sled_size.y - 48, sled_t + 2], 4);                                         // airflow
    }
}

module damper() {
    difference() { cylinder(d = damper_d, h = damper_h); translate([0, 0, 2]) cylinder(d = damper_peg_d - 0.4, h = damper_h); }
}

// ───────────────────────── TPU armour: front/rear frames + four edge bumpers ─────────────────────────
// Every outside edge is covered. Each FRAME wraps one face's 4 edges and 16 mm onto the top, bottom and sides, around a window that
// leaves the panel band and its rim clear. The four EDGE BUMPERS cover the long front-to-rear edges, full size (6 mm, the frames'
// profile); a dovetail tongue on the side leg slides into a groove in the side wall, and at each end the top/bottom leg carries on
// as a 2.5 mm tenon plate that tucks into a slot under the frame's wrap. Fit order: edge bumpers (slide in) → frames (6 screws each).
// Both shapes print without supports: frames face down; edge bumpers on the side leg's outer face (45° bevels where they meet the bed).

// 2D rounded rectangle whose corners bevel 45° on the half next to the vertical sides (the same corner as the edge bumpers)
module td_rect(x0, x1, z0, z1, r) hull() for (left = [true, false], top = [true, false]) {
    c = [left ? x0 + r : x1 - r, top ? z1 - r : z0 + r];
    translate(c) circle(r = r);
    translate([left ? x0 : x1 - 0.01, c.y + (top ? 0.414 * r : -0.414 * r - 0.01)]) square(0.01);
}
// outward size of the frame's outer surface at depth y (case y; the face is at y = -bumper_t) over a case edge of radius a:
// a rounded fillet at the face, a 45° bevel (prints face down) and then a round concentric with the case edge
function eprof(y, a, t = bumper_t, f = bumper_fillet) =
    let(R = a + t, e0 = R * sqrt(2) - 2 * a - t, yc = -t + f, ec = e0 - f * (sqrt(2) - 1))
    y < yc - f / sqrt(2) ? ec + f * sqrt(max(0, 1 - pow((yc - y) / f, 2)))
    : y < a - R / sqrt(2) ? e0 + (y + t)
    : y < a ? sqrt(R * R - (a - y) * (a - y)) - a
    : t;
function eend(y, L = bumper_leg, t = bumper_t, f = bumper_fillet) = y <= L - f ? t : t - f + sqrt(max(0, f * f - pow(y - (L - f), 2)));

module case_solid(c = 0) translate([-c, -c, -c]) rbox_f([outer.x + 2 * c, outer.y + 2 * c, Hc + 2 * c], corner_r + c, fb = edge_fillet + c, ft = lid_ft + c);

module frame_env() {   // the convex outer shape of the front frame
    t = bumper_t; f = bumper_fillet; L = bumper_leg;
    ys = concat([for (i = [0:5]) -t + f - f * cos(45 * i / 5)],
                [for (y = [-t + f * (1 - 1 / sqrt(2)) + 0.25 : 0.5 : corner_r]) y],
                [for (i = [0:6]) L - f + f * sin(90 * i / 6)]);
    hull() for (y = ys) let(ex = min(eprof(y, corner_r), eend(y)), ez = min(eprof(y, edge_r), eend(y)))
        xz_extrude(y - 0.005, y + 0.005) td_rect(-ex, outer.x + ex, -ez, Hc + ez, edge_r + max(0, min(ex, ez)));
}
module frame_window() {   // the window, with a rounded 45° lead-in at the outer face
    t = bumper_t; ch = frame_chamfer; f = bumper_fillet; o = frame_open;
    prof = concat([[-1, ch + 0.414 * f + 1]],
                  [for (i = [0:4]) let(a = 45 * i / 4) [f - f * cos(a), ch + 0.414 * f - f * sin(a)]],
                  [for (i = [0:4]) let(b = 45 - 45 * i / 4) [ch + 0.414 * f - f * sin(b), f - f * cos(b)]], [[t + 1, 0]]);
    for (i = [0:len(prof) - 2]) hull() for (p = [prof[i], prof[i + 1]])
        xz_extrude(-t + p[0] - 0.005, -t + p[0] + 0.005) offset(r = p[1]) translate([o[0], o[2]]) rr2d([o[1] - o[0], o[3] - o[2]], frame_open_r);
}
module frame_screw(p, top) {   // through hole + counterbore, teardrops pointing up in the print (+y), so they print without support
    t = bumper_t; cb = bumper_cbore;
    translate([p.x, p.y, 0]) {
        translate([0, 0, top ? Hc - 1 : -t - 1]) linear_extrude(t + 2) teardrop2d(m3_clear);
        translate([0, 0, top ? Hc + t - cb[1] : -t - 1]) linear_extrude(cb[1] + 1) teardrop2d(cb[0]);
        translate([0, 0, top ? Hc + t - 0.8 : -t - 0.01]) cylinder(d1 = top ? cb[0] : cb[0] + 1.6, d2 = top ? cb[0] + 1.6 : cb[0], h = 0.81);
    }
}

// Edge-bumper local coordinates: u = out from the side face, v = out from the top (or bottom) face, w = case y.
function edge_m(i) = let(sx = i % 2 == 0 ? -1 : 1, x0 = i % 2 == 0 ? 0 : outer.x, top = i < 2)
    [[sx, 0, 0, x0], [0, 0, 1, 0], [0, top ? 1 : -1, 0, top ? Hc : 0], [0, 0, 0, 1]];
module at_edge(i) multmatrix(edge_m(i)) children();           // 0 top-left · 1 top-right · 2 bottom-left · 3 bottom-right
module w_extrude(w0, w1) translate([0, 0, w0]) linear_extrude(w1 - w0) children();
eb_screw_w = [corner_r - 1, outer.y - corner_r + 1];           // corner screws in edge-bumper coordinates (u = -(corner_r - 1))
module eb_outer2d(tau = bumper_t, L = edge_leg) let(R = edge_r + tau, f = bumper_fillet) hull() {
    translate([-edge_r, -edge_r]) circle(r = R);
    translate([tau - 0.01, -edge_r + 0.414 * R]) square(0.01);   // 45° bevel on the side-leg half (the print bed side)
    translate([tau - f, -L + f]) circle(r = f);
    translate([-L + f, tau - f]) circle(r = f);
    translate([-L, -L]) square(0.01);
}
module eb_inner2d(c = bumper_clear) hull() {                     // the case edge, plus clearance
    translate([-edge_r, -edge_r]) circle(r = edge_r + c);
    translate([-60, -60]) square([60 + c, 60 - edge_r]);
    translate([-60, -60]) square([60 - edge_r, 60 + c]);
}
module dove2d(c) let(w = dove[0], h = dove[1], zc = -dove[2])     // c = 0: the groove in the case · c = dove[3]: the tongue
    offset(delta = -c) polygon([[1, zc - w / 2], [0, zc - w / 2], [-h, zc - w / 2 - h], [-h, zc + w / 2 + h], [0, zc + w / 2], [1, zc + w / 2]]);
module eb_notch(r) for (w = eb_screw_w) translate([-(corner_r - 1), 20, w]) rotate([90, 0, 0]) linear_extrude(40) rotate(90) teardrop2d(2 * r);
module tenon_plate(tau, L, tt, w0, w1, f = 0.8) intersection() {
    hull() for (p = fillet_steps(f), e = [0, 1]) let(w = e == 0 ? w0 + p[0] : w1 - p[0]) w_extrude(w - 0.005, w + 0.005) eb_outer2d(tau - p[1], L - p[1]);
    translate([-L - 1, bumper_clear - 0.01, w0 - 1]) cube([L + tau + 2, tt - bumper_clear + 0.01, w1 - w0 + 2]);
}

module edge_bumper() {
    // Full-size TPU edge bumper (local coordinates; the same part on all four long edges). Body: 6 mm thick, 16 mm legs,
    // between the frames. Ends: the top/bottom leg carries on as a 2.5 mm tenon plate under the frame, notched round the
    // corner screw. Tongue: a 45° dovetail on the side leg slides along the groove in the side wall.
    t = bumper_t; L = edge_leg; f = bumper_fillet;
    w0 = bumper_leg + bumper_clear; w1 = outer.y - w0;
    difference() {
        union() {
            hull() for (p = fillet_steps(f), e = [0, 1]) let(w = e == 0 ? w0 + p[0] : w1 - p[0])
                w_extrude(w - 0.005, w + 0.005) eb_outer2d(t - p[1], L - p[1]);
            tenon_plate(t, L, tenon[0], tenon[1], outer.y - tenon[1]);
        }
        w_extrude(-1, outer.y + 1) eb_inner2d();
        eb_notch(frame_boss + tenon[2]);
    }
    intersection() {   // tongue, with 45° lead-ins at both ends
        w_extrude(w0 + f, w1 - f) dove2d(dove[3]);
        hull() {
            translate([-0.3, -40, w0 + f]) cube([2.3, 80, 0.01]);
            translate([-5, -40, w0 + f + 1.6]) cube([7, 80, w1 - w0 - 2 * f - 3.2]);
            translate([-0.3, -40, w1 - f - 0.01]) cube([2.3, 80, 0.01]);
        }
    }
}
module tenon_slot() difference() {   // the frame's slot for one tenon (local coordinates), open to the outside and the back
    w0 = tenon[1] - tenon[2];
    intersection() {
        w_extrude(w0, bumper_leg + 1) eb_outer2d(bumper_t + 1, edge_leg + tenon[2]);
        translate([-edge_leg - 2, bumper_clear - 0.1, w0 - 1]) cube([edge_leg + bumper_t + 4, tenon[0] + tenon[2] - bumper_clear + 0.1, bumper_leg + 3]);
    }
    eb_notch(frame_boss);
}

module frame() {
    // FRONT frame, in place. The rear frame is the same part turned round (screw_pts are symmetric). TPU 95A, printed face down.
    // Held by 3 screws on top (the 2 corner and the middle lid screws, M3×12 through the frame and the lid into the boss inserts)
    // and 3 underneath (M3×8 into inserts in the bottom of the same bosses); heads sunk in counterbores.
    difference() {
        frame_env();
        case_solid(bumper_clear);
        frame_window();
        for (i = [0:3]) at_edge(i) tenon_slot();
        for (p = frame_pts, top = [true, false]) frame_screw(p, top);
    }
}
module frames() { frame(); translate([outer.x, outer.y, 0]) rotate([0, 0, 180]) frame(); }
module edge_bumpers() for (i = [0:3]) at_edge(i) edge_bumper();

// ── checks for the armour (called from layout_check / panel_check) ──
// rectangles in (y, z) on the right wall: louver outer faces including the 1 mm rim rounding
function louver_rects() = [for (y = louver_y) [y - 1, louver_z - 1, y + louver_size[0] + 1, louver_z + louver_size[1] + 1]];
// smallest tilt (°) at which the unfolded SDR whip clears the rear frame's window edge
function whip_tilt(a = 0) = let(K = [sdr_whip_offset - rear_depth, floor_t + sma_at[0].y], r = sdr_whip_d / 2,
        P = [[bumper_t - frame_chamfer, frame_open[3]], [bumper_t, frame_open[3] + frame_chamfer]],
        ok = min([for (p = P) (p[1] - K[1]) * sin(a) - (p[0] - K[0]) * cos(a)]) >= r)
    ok || a >= 60 ? a : whip_tilt(a + 0.5);
module bumper_check() {
    // louvers: clear of the frames, the edge bumpers, the side screw bosses and the Wi-Fi slot, with webs between them
    gz = [dove[2] - dove[0] / 2 - dove[1], dove[2] + dove[0] / 2 + dove[1]];          // groove band from the top/bottom face
    for (i = [0:len(louver_y) - 1]) let(l = louver_rects()[i], n = str("WARNING: louver ", i + 1, " (y ", louver_y[i], ") ")) {
        if (l[0] < bumper_leg + 1 || l[2] > outer.y - bumper_leg - 1) echo(str(n, "is under a frame"));
        if (l[1] < edge_leg + 1 || l[3] > Hc - edge_leg - 1) echo(str(n, "is under an edge bumper"));
        if (l[1] < gz[1] + 1 || l[3] > Hc - gz[1] - 1) echo(str(n, "cuts the edge-bumper groove"));
        for (p = screw_pts) if (p.x > outer.x - wall - boss_d && l[0] < p.y + boss_d / 2 && l[2] > p.y - boss_d / 2) echo(str(n, "hits the screw boss at y ", p.y));
        if (l[2] > wifi_at.y) echo(str(n, "reaches the Wi-Fi slot"));
        if (i > 0 && louver_y[i] - louver_y[i - 1] - louver_size[0] - 2 < 2) echo(str(n, "web to the previous louver < 2 mm"));
    }
    // the fan and the edge bumpers / grooves (left wall)
    fz = [floor_t + fan_at.y - fan / 2, floor_t + fan_at.y + fan / 2];
    if (fz[0] < edge_leg + 1 || fz[1] > Hc - edge_leg - 1) echo("WARNING: the fan grille is under an edge bumper");
    if (fan_at.x - fan / 2 < bumper_leg + 1 || fan_at.x + fan / 2 > outer.y - bumper_leg - 1) echo("WARNING: the fan grille is under a frame");
    // grooves fit the side walls and stay under the bumpers
    if (dove[1] > wall - 2) echo("WARNING: the edge-bumper groove leaves less than 2 mm of wall");
    if (gz[1] > edge_leg - 2) echo("WARNING: the edge-bumper groove runs out from under the bumper");
    // lid artwork and QR sticker clear of the bumpers
    if (outer.x - 20 > outer.x - edge_leg - 1 || 18 < bumper_leg + 1) echo("WARNING: the QR sticker recess is under a bumper");
    if (21 < edge_leg + 1) echo("WARNING: the lid logo is under an edge bumper");
    // screws: counterbores clear of the tenon slots, and thread engagement in the 5.7 mm inserts
    if (frame_boss < bumper_cbore[0] / 2 + 0.5) echo("WARNING: frame counterbore breaks into a tenon slot");
    top_eng = 12 - (bumper_t - bumper_cbore[1] + bumper_clear + lid_t + 0.1);
    bot_eng = 8 - (bumper_t - bumper_cbore[1] + bumper_clear);
    if (top_eng < 3.5 || top_eng > insert_depth - 0.3) echo(str("WARNING: M3×12 top frame screws engage ", top_eng, " mm"));
    if (bot_eng < 3.5 || bot_eng > insert_depth - 0.3) echo(str("WARNING: M3×8 bottom frame screws engage ", bot_eng, " mm"));
    side_eng = 6 - (lid_t - 2.2);   // the 2 hidden side lid screws, M3×6 in the lid counterbores
    if (side_eng < 3.5 || side_eng > insert_depth - 0.3) echo(str("WARNING: M3×6 side lid screws engage ", side_eng, " mm"));
    // print beds
    ff = [outer.x + 2 * bumper_t, Hc + 2 * bumper_t]; ef = [outer.y - 2 * tenon[1], edge_leg + bumper_t];
    if (max(ff) > 220 || max(ef) > 220) echo("WARNING: a bumper part doesn't fit a 220 × 220 mm bed");
    wt = whip_tilt();
    if (wt > 20) echo(str("WARNING: the SDR whip must tilt back ", wt, "° to clear the rear frame"));
    echo(str("Bumpers: 2 frames ", ff.x, " × ", ff.y, " × ", bumper_t + bumper_leg, " mm (print face down), 4 edge bumpers ", ef.x, " × ", ef.y, " × ", ef.y,
             " mm; louvers at y ", louver_y, ", z ", louver_z, "-", louver_z + louver_size[1], " (outer face); screws engage ", top_eng, " / ", bot_eng, " / ", side_eng,
             " mm (M3×12 frame tops / M3×8 frame bottoms / M3×6 side lid screws); the SDR whip clears the rear frame when tilted back ≥ ", wt, "°"));
}
module frame_window_check() {
    o = frame_open;
    // the band rims (at the case face) must sit inside the window
    rims = [["front band", [front_band[0] - rim_fillet, floor_t + front_band[2] - front_depth - rim_fillet, front_band[1] + rim_fillet, floor_t + front_band[3] + front_depth + rim_fillet]],
            ["rear band", [rband_x[0] - rim_fillet, floor_t + rear_band[2] - 2 - rim_fillet, rband_x[1] + rim_fillet, floor_t + rear_band[3] + rear_depth + rim_fillet]]];
    // connectors and the room they need, as [x, z, half-width, half-height] in box coordinates
    parts = [["screen", [oled_at.x, floor_t + oled_at.y, oled_window.x / 2, oled_window.y / 2]],
             ["knob and fingers", [encoder_at.x, floor_t + encoder_at.y, 14, 14]],
             ["button", [button_at.x, floor_t + button_at.y, 8, 8]], ["LED", [led_at.x, floor_t + led_at.y, 4, 4]],
             ["USB-A plugs", [usba_at[0].x, floor_t + front_cl, 9, abs(usba_at[0].y - usba_at[1].y) / 2 + 5]],
             ["GX16 finger room", [rv(gx16_at.x), floor_t + gx16_at.y, gx16_room, gx16_room]],
             ["Ethernet plug", [rv(rj45_at.x), floor_t + rj45_at.y, 9, 9]], ["USB-C plug", [rv(usbc_at.x), floor_t + usbc_at.y, 7, 5]],
             ["SDR jack", [rv(sma_at[0].x), floor_t + sma_at[0].y, 5, 5]],
             ["SDR 2 plug", [rv(sdr2_jack.x), floor_t + sdr2_jack.y, 6, 6]], ["sensor plug", [rv(sensor_sock_at.x), floor_t + sensor_sock_at.y, 7, 7]],
             ["folded whip", [rv(sma_at[0].x + sdr_whip_len / 2), floor_t + sma_at[0].y, sdr_whip_len / 2 + 5, sdr_whip_d / 2]],
             ["whip clips", [rv((sdr_clip_x[0] + sdr_clip_x[1]) / 2), floor_t + sma_at[0].y, (sdr_clip_x[1] - sdr_clip_x[0]) / 2 + 18, 6]]];
    rects = concat(rims, [for (p = parts) [p[0], [p[1][0] - p[1][2], p[1][1] - p[1][3], p[1][0] + p[1][2], p[1][1] + p[1][3]]]]);
    m = [for (r = rects) min(r[1][0] - o[0], r[1][1] - o[2], o[1] - r[1][2], o[3] - r[1][3])];
    for (i = [0:len(rects) - 1]) if (m[i] < (i < 2 ? 0.75 : 1)) echo(str("WARNING: the frame window covers the ", rects[i][0], " (", m[i], " mm)"));
    if (o[0] < bumper_t + corner_r || o[2] < 8 || Hc - o[3] < 8) echo("WARNING: the frame border is too narrow to hold");
    echo(str("Frame window ", o, ": margins to the band rims ", [m[0], m[1]], " mm, to the connectors ≥ ", min([for (i = [2:len(m) - 1]) m[i]]), " mm"));
}

module gasket() {
    h = gasket_print[1];
    w = gasket_print[0];
    difference() {
        translate([wall / 2 - w / 2, wall / 2 - w / 2, 0]) rbox([outer.x - wall + w, outer.y - wall + w, h], corner_r - wall / 2 + w / 2);
        translate([wall / 2 + w / 2, wall / 2 + w / 2, -1]) rbox([outer.x - wall - w, outer.y - wall - w, h + 2], corner_r - wall / 2 - w / 2);
        for (p = screw_pts) translate([p.x, p.y, -1]) cylinder(d = m3_clear + 1, h = h + 2);
    }
}

module sdr_clip() {
    // Snap ring for the folded SDR whip. Screws sit BESIDE the ring (±13 mm), so nothing cuts through it.
    // Print in TPU (flexes without cracking), lying on its back. All edges rounded. The crown of the ring is cut flat so the
    // clip stays inside the bumpers (clip_top); the snap lips below it are untouched.
    ring_id = sdr_whip_d + 0.4; ring_w = 2.4; reach = sdr_whip_offset; L = 10;
    intersection() { difference() {
        union() {
            translate([-18, -5, 0]) rbox_f([36, 10, 3], 3, ft = 1);
            hull() for (x = [-3.5, 3.5], y = [-2.5, 2.5]) translate([x, y, 0]) cylinder(r = 1.5, h = reach);
            translate([0, 0, reach]) rotate([0, 90, 0]) translate([0, 0, -L / 2])
                rotate_extrude($fn = 48) translate([ring_id / 2, 0]) offset(r = 0.8) offset(delta = -0.8) square([ring_w, L]);
        }
        translate([0, 0, reach]) rotate([0, 90, 0]) cylinder(d = ring_id, h = L + 2, center = true, $fn = 48);
        translate([-L / 2 - 1, -ring_id * 0.36, reach + ring_id * 0.2]) cube([L + 2, ring_id * 0.72, ring_id]);
        for (dx = [-clip_screw_dx, clip_screw_dx]) translate([dx, 0, -1]) { cylinder(d = m3_clear, h = 5); translate([0, 0, 3.01]) cylinder(d = m3_head_d, h = 3); }
    }
    translate([-30, -30, -1]) cube([60, 60, reach + clip_top + 1]); }
}

// ───────────────────────── second-SDR shelf ─────────────────────────
// A deck over the first dongle on two screwed posts (right) and a leg (left). The second dongle is
// zip-tied on top; its USB goes to the hub's 4th port and a right-angle SMA pigtail runs back to the
// spare rear jack along the gap beside the Wi-Fi adapter. Modelled in place; exported deck-down.
module sdr_shelf() {
    z0 = floor_t + sdr_box.z + shelf_gap;               // deck underside
    x0 = shelf_leg[0]; x1 = shelf_posts[0].x + shelf_post_d / 2;
    y0 = sdr2_at.y; y1 = sdr2_at.y + sdr2_box.y;
    difference() {
        union() {
            translate([x0, y0, z0]) cube([x1 - x0, y1 - y0, shelf_t]);
            translate([x0, shelf_leg[1], floor_t]) cube([shelf_leg[2], shelf_leg[3], z0 - floor_t + 0.01]);
            for (p = shelf_posts) translate([p.x, p.y, floor_t + shelf_boss_h + 0.2]) cylinder(d = shelf_post_d, h = z0 + shelf_t - floor_t - shelf_boss_h - 0.2);
        }
        for (p = shelf_posts) translate([p.x, p.y, 0]) {
            cylinder(d = m3_clear, h = 100);
            translate([0, 0, z0 + shelf_t - shelf_cbore]) cylinder(d = 6.4, h = 10);
        }
        // vents, so the first dongle's heat rises past the deck
        for (y = [y0 + 8, y0 + 38]) translate([sdr2_at.x + 4, y, z0 - 1]) cube([sdr2_box.x - 8, 24, shelf_t + 2]);
        // zip-tie slots either side of the second dongle
        for (y = [y0 + 4, y1 - 7], x = [sdr2_at.x - 5, sdr2_at.x + sdr2_box.x + 2]) translate([x, y, z0 - 1]) cube([3, 3, shelf_t + 2]);
    }
}

// ───────────────────────── fit check ─────────────────────────
module envelopes() {
    for (d = damper_pts) translate([d.x, d.y, floor_t]) color("Black", 0.8) cylinder(d = damper_d, h = damper_h);
    translate([sled_xy.x, sled_xy.y, floor_t + damper_h]) color("Teal", 0.8) sled();
    translate([ups_at.x, ups_at.y, stack_z0]) color("SteelBlue", 0.6) cube([ups_size.x, ups_size.y, ups_h]);
    translate([pi_at.x, pi_at.y, stack_z0 + ups_h]) color("Green", 0.7) cube([pi_board.x, pi_board.y, pi_h]);
    translate([pi_at.x, pi_at.y, stack_z0 + ups_h + pi_h]) color("Purple", 0.7) cube([pi_board.x, pi_board.y, nvme_h]);
    translate([pi_at.x, pi_at.y, stack_top]) color("Pink", 0.6) cube([pi_board.x, pi_board.y, foam_t]);
    translate([pi_at.x + pi_board.x, pi_at.y, stack_z0 + ups_h]) color("Red", 0.25) cube([port_plug_room, pi_board.y, pi_h]);  // plug room
    translate([wifi_at.x, wifi_at.y + 0.8, floor_t + 1]) color("Orange", 0.7) cube(wifi_box);
    translate([sdr_at.x, sdr_at.y, floor_t]) color("Gold", 0.7) cube(sdr_box);
    if (sdr2 != "none") {
        color("SlateGray", 0.8) sdr_shelf();
        translate([sdr2_at.x, sdr2_at.y, floor_t + sdr2_z]) color("Goldenrod", 0.7) cube(sdr2_box);
    }
    translate([hub_at.x, hub_at.y, floor_t]) color("Gray", 0.7) cube(hub_box);
    translate([rak_at.x, rak_at.y, floor_t]) color("Tomato", 0.7) cube(rak_box);
    translate([power_tray_at.x, power_tray_at.y, floor_t + tray_boss[1]]) color("Olive", 0.7) cube([power_tray.x, power_tray.y, 14]);
    translate([oled_at.x - 18, wall + 3, floor_t + oled_at.y - 17]) color("White", 0.8) cube([36, 6, 34]);
    translate([wall, fan_at.x - fan / 2, floor_t + fan_at.y - fan / 2]) color("DimGray", 0.6) cube([10, fan, fan]);
}

function overlap(a, b) = a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];
function box2(at, size) = [at.x, at.y, at.x + size.x, at.y + size.y];
module layout_check() {
    lid_in = [wall + clear + 2, wall + clear + 2, outer.x - wall - clear - 2, outer.y - wall - clear - 2];
    inner_box = [wall, wall, outer.x - wall, outer.y - wall];
    bosses = [for (p = screw_pts) [p.x - boss_d / 2, p.y - boss_d / 2, p.x + boss_d / 2, p.y + boss_d / 2]];
    sled = box2(sled_xy, sled_size);
    ups = box2(ups_at, ups_size);
    plug = [pi_at.x + pi_board.x, pi_at.y, pi_at.x + pi_board.x + port_plug_room, pi_at.y + pi_board.y];
    wifi = [wifi_at.x - 1.6, wifi_at.y, wifi_at.x + wifi_box.x + 2.2, wifi_at.y + wifi_box.y + 1.6];
    tray = box2(power_tray_at, power_tray);
    cups = [for (d = damper_pts) [d.x - 8.2, d.y - 8.2, d.x + 8.2, d.y + 8.2]];
    fanb = [wall, fan_at.x - fan / 2, wall + 10, fan_at.x + fan / 2];
    rear_y = outer.y - rear_depth - rear_well_t - 20;
    rear_bodies = [[rv(usbc_at.x) - 12, outer.y - rear_depth - rear_well_t - 20, rv(usbc_at.x) + 12, outer.y - wall],
                   [rv(rj45_at.x) - 12, outer.y - rear_depth - rear_well_t - 28, rv(rj45_at.x) + 12, outer.y - wall],
                   [rv(gx16_at.x) - 12, outer.y - rear_depth - rear_well_t - 15, rv(gx16_at.x) + 12, outer.y - wall]];
    rear_low = [floor_t + rj45_at.y - 9, floor_t + usbc_at.y - 5, floor_t + gx16_at.y - 9];   // bottom of each body
    chan_back = [rband_x[0] - rear_well_t, outer.y - rear_depth - rear_well_t, rband_x[1] + rear_well_t, outer.y - wall];
    band = [front_band[0] - front_well_t, wall, front_band[1] + front_well_t, front_depth + front_well_t];
    oled_pcb = [oled_at.x - 18, front_depth + front_well_t, oled_at.x + 18, front_depth + front_well_t + 6];
    if (overlap(band, tray) || overlap(band, box2(rak_at, rak_box))) echo("WARNING: front band runs into the tray or the RAK node");
    if (overlap(oled_pcb, tray) && oled_at.y - 17 < tray_boss[1] + 14) echo("WARNING: screen module sits on the power tray");
    if (overlap(chan_back, wifi)) echo("WARNING: rear band runs into the Wi-Fi slot");
    if (sma_at[0].x - rear_band[0] < 9 || sma_at[0].x + sdr_whip_len > rear_band[1]) echo("WARNING: folded whip doesn't fit the rear band");
    for (c = sdr_clip_x) if (c - 18 < rear_band[0] || c + 18 > rear_band[1]) echo("WARNING: a whip clip is outside the rear band");
    if (sma_at[0].y - sdr_whip_d / 2 - 2 < gx16_at.y + gx16_room) echo("WARNING: the folded whip passes through the GX16 finger room");
    if (gx16_at.y - 9.5 < rear_band[2] || sma_at[0].y + 7.1 > rear_band[3]) echo("WARNING: rear parts don't fit the band's height");
    floor_items = [["sled", sled], ["Wi-Fi slot", wifi], ["SDR", box2(sdr_at, sdr_box)], ["hub", box2(hub_at, hub_box)],
                   ["RAK node", box2(rak_at, rak_box)], ["power tray", tray], ["fan", fanb]];
    for (i = [0:len(floor_items) - 2], j = [i + 1:len(floor_items) - 1])
        if (overlap(floor_items[i][1], floor_items[j][1])) echo(str("WARNING: ", floor_items[i][0], " overlaps ", floor_items[j][0]));
    for (f = floor_items, b = bosses) if (overlap(f[1], b)) echo(str("WARNING: ", f[0], " hits a screw boss"));
    for (f = floor_items) if (f[1][0] < inner_box[0] - 0.01 || f[1][1] < inner_box[1] - 0.01 || f[1][2] > inner_box[2] + 0.01 || f[1][3] > inner_box[3] + 0.01)
        echo(str("WARNING: ", f[0], " is outside the walls"));
    for (c = cups, f = [["power tray", tray], ["SDR", box2(sdr_at, sdr_box)]]) if (overlap(c, f[1])) echo(str("WARNING: damper cup hits ", f[0]));
    low_h = [["SDR", floor_t + sdr_box.z], ["hub", floor_t + hub_box.z], ["RAK node", floor_t + rak_box.z]];
    for (i = [0:len(rear_bodies) - 1], f = floor_items) if (f[0] != "fan" && overlap(rear_bodies[i], f[1])) {
        lh = [for (l = low_h) if (l[0] == f[0]) l[1]];
        if (len(lh) == 0 || rear_low[i] < lh[0]) echo(str("WARNING: rear connector body hits ", f[0]));
    }
    if (overlap(plug, wifi)) echo("WARNING: Wi-Fi slot blocks the Pi's plug room");
    // second SDR on its shelf
    s2_top = floor_t + sdr2_z + sdr2_box.z;
    shelf_fp = [shelf_leg[0], sdr2_at.y, shelf_posts[0].x + shelf_post_d / 2, sdr2_at.y + sdr2_box.y];
    posts = [for (p = shelf_posts) [p.x - shelf_post_d / 2, p.y - shelf_post_d / 2, p.x + shelf_post_d / 2, p.y + shelf_post_d / 2]];
    leg = [shelf_leg[0], shelf_leg[1], shelf_leg[0] + shelf_leg[2], shelf_leg[1] + shelf_leg[3]];
    feet = concat([["leg", leg]], [for (p = posts) ["post", p]]);
    sdr_anchors = [[sdr_at.x - 6, sdr_at.y + 10], [sdr_at.x - 6, sdr_at.y + 50], [sdr_at.x + sdr_box.x + 1, sdr_at.y + 30],
                   [hub_at.x + hub_box.x + 1, hub_at.y + 8]];
    for (i = [0:len(rear_bodies) - 1]) if (overlap(rear_bodies[i], shelf_fp) && rear_low[i] < s2_top) echo("WARNING: second SDR hits a rear connector body");
    for (f = feet, g = [["sled", sled], ["SDR", box2(sdr_at, sdr_box)], ["hub", box2(hub_at, hub_box)], ["Wi-Fi slot", wifi], ["RAK node", box2(rak_at, rak_box)]])
        if (overlap(f[1], g[1])) echo(str("WARNING: shelf ", f[0], " hits the ", g[0]));
    for (f = feet, c = cups) if (overlap(f[1], c)) echo("WARNING: shelf hits a damper cup");
    for (f = feet, a = sdr_anchors) if (overlap(f[1], [a.x, a.y, a.x + 9, a.y + 6])) echo(str("WARNING: shelf ", f[0], " sits on a zip-tie anchor"));
    for (f = feet, b = bosses) if (overlap(f[1], b)) echo(str("WARNING: shelf ", f[0], " hits a screw boss"));
    if (shelf_fp[2] > wifi[0]) echo("WARNING: shelf runs into the Wi-Fi slot");
    if (hub_box.z > sdr_box.z + shelf_gap) echo("WARNING: the hub is taller than the shelf clearance");
    if (overlap(plug, shelf_fp) && stack_z0 + ups_h - s2_top < 2) echo(str("WARNING: second SDR is ", stack_z0 + ups_h - s2_top, " mm below the Pi's plug room"));
    if (s2_top > outer.z - lip_h - 10) echo("WARNING: second SDR too close to the lid");
    // the second dongle's SMA end (with its right-angle plug) vs the RJ45 jack body
    rj_body = [rv(rj45_at.x) - rj45_cut.x / 2 - 1.5, outer.y - rear_depth - rear_well_t - rj45_body_depth, rv(rj45_at.x) + rj45_cut.x / 2 + 1.5, outer.y];
    s2_end = [sdr2_at.x, sdr2_at.y + sdr2_box.y, sdr2_at.x + sdr2_box.x + sdr2_pigtail, sdr2_at.y + sdr2_box.y + sdr2_pigtail];
    if (overlap(rj_body, s2_end)) echo("WARNING: the second dongle's SMA plug runs into the RJ45 jack body");
    // rear panel: the SDR 2 jack vs its neighbours (nut/knurl radius 5, relief radius 6, screw heads radius 2.75)
    if (sdr2 != "none") {
        nb = [["Ethernet ear", [rj45_at.x + rj45_screw / 2, rj45_at.y], 2.75 + 5], ["USB-C ear", [usbc_at.x - usbc_screw / 2, usbc_at.y], 2.75 + 5],
              ["Ethernet jack", [rj45_at.x + rj45_cut.x / 2, sdr2_jack.y], 5], ["USB-C jack", [usbc_at.x - usbc_cut.x / 2, sdr2_jack.y], 5]];
        gaps2 = [for (n = nb) norm(n[1] - sdr2_jack) - n[2]];
        for (i = [0:len(nb) - 1]) if (gaps2[i] < 2) echo(str("WARNING: SDR 2 jack is ", gaps2[i], " mm from the ", nb[i][0]));
        whip_gap = (sma_at[0].y - sdr_whip_d / 2) - (sdr2_jack.y + 5);
        if (whip_gap < 4) echo(str("WARNING: SDR 2 jack is ", whip_gap, " mm under the folded whip"));
        if (sdr2_jack.y - 6 < rear_band[2] + 2) echo("WARNING: SDR 2 jack too close to the band's lower edge");
        for (cx = sdr_clip_x, dx = [-clip_screw_dx, clip_screw_dx]) if (abs(cx + dx - sdr2_jack.x) < 4.5 + 6 && sdr2_jack.y + 6 > sma_at[0].y - 11.5)
            echo("WARNING: SDR 2 jack runs into a whip-clip boss");
        echo(str("SDR 2 jack: clearances ", [for (g = gaps2) round(g * 10) / 10], " mm to the ear screws / jacks, ", whip_gap, " mm under the folded whip"));
    }
    if (lora_ext && sdr2 != "none") echo("NOTE: lora_ext and sdr2 are both on the rear band");
    if (sensor_sock) {
        r = sensor_sock_nut / 2;
        sg = [(sensor_sock_at.x - r) - (usbc_at.x + usbc_screw / 2 + 2.75),          // USB-C ear screw head
              (gx16_at.x - 20 / 2 / cos(30)) - (sensor_sock_at.x + r),                // GX16 nut corners
              (gx16_at.x - gx16_room) - (sensor_sock_at.x + r),                        // GX16 lock-ring finger room
              (sma_at[0].y - sdr_whip_d / 2) - (sensor_sock_at.y + r),                 // folded whip
              (sensor_sock_at.y - r) - rear_band[2]];                                  // band's lower edge
        names = ["USB-C ear", "GX16 nut", "GX16 finger room", "folded whip", "band edge"];
        for (i = [0:len(sg) - 1]) if (sg[i] < 1.5) echo(str("WARNING: sensor socket is ", sg[i], " mm from the ", names[i]));
        for (cx = sdr_clip_x, dx = [-clip_screw_dx, clip_screw_dx]) if (abs(cx + dx - sensor_sock_at.x) < 4.5 + r && sensor_sock_at.y + r > sma_at[0].y - 11.5)
            echo("WARNING: sensor socket runs into a whip-clip boss");
        sbody = [rv(sensor_sock_at.x) - 7, outer.y - rear_depth - rear_well_t - 18, rv(sensor_sock_at.x) + 7, outer.y - wall];
        for (i = [0:len(rear_bodies) - 1]) if (overlap(sbody, rear_bodies[i])) echo("WARNING: sensor socket body hits another rear connector body");
        if (overlap(sbody, sled)) echo("WARNING: sensor socket body reaches the sled");
        echo(str("Sensor socket (M8): clearances ", [for (g = sg) round(g * 10) / 10], " mm to ", names));
    }
    echo(str("Second SDR: shelf deck at ", floor_t + sdr_box.z + shelf_gap, "-", floor_t + sdr2_z, " mm, dongle top ", s2_top,
             " mm, ", stack_z0 + ups_h - s2_top, " mm under the Pi's plug room (point the 90° USB adapters up); rear jack: ", sdr2));
    if (fanb[2] + 10 > ups[0]) echo(str("WARNING: fan within 10 mm of the cells (", ups[0] - fanb[2], " mm)"));
    if (plug[2] > inner_box[2]) echo("WARNING: no plug room before the right wall");
    // lid side
    ants = [["Wi-Fi A", [wifi_a_at.x - 1.2, wifi_a_at.y - 1.2, wifi_a_at.x + wifi_fpc.x + 1.2, wifi_a_at.y + wifi_fpc.y + 1.2]],
            ["Wi-Fi B", [wifi_b_at.x - 1.2, wifi_b_at.y - 1.2, wifi_b_at.x + wifi_fpc.y + 1.2, wifi_b_at.y + wifi_fpc.x + 1.2]],
            ["LoRa", [lora_at.x - 1.2, lora_at.y - 1.2, lora_at.x + lora_fpc.x + 1.2, lora_at.y + lora_fpc.y + 1.2]],
            ["GPS", [gps_at.x, gps_at.y, gps_at.x + gps_box.x + 4, gps_at.y + gps_box.y + 4]]];
    for (a = ants) {
        if (a[1][0] < lid_in[0] || a[1][1] < lid_in[1] || a[1][2] > lid_in[2] || a[1][3] > lid_in[3]) echo(str("WARNING: ", a[0], " runs into the lid lip"));
        if (a[0] != "GPS" && overlap(a[1], sled)) echo(str("WARNING: ", a[0], " sits over the metal stack"));   // a GPS patch faces up; metal below is its ground plane
        for (b = bosses) if (overlap(a[1], b)) echo(str("WARNING: ", a[0], " hits a screw boss"));
    }
    for (i = [0:len(ants) - 2], j = [i + 1:len(ants) - 1]) if (overlap(ants[i][1], ants[j][1])) echo(str("WARNING: ", ants[i][0], " overlaps ", ants[j][0]));
    for (an = lid_anchors, a = ants) if (overlap([an.x, an.y, an.x + 9, an.y + 6], a[1])) echo(str("WARNING: a lid anchor sits on ", a[0]));
    gps_bottom = outer.z - 6 - gps_box.z;
    gps2 = ants[3][1];
    pi_fp = [pi_at.x, pi_at.y, pi_at.x + pi_board.x, pi_at.y + pi_board.y];
    band_back = [front_band[0] - front_well_t, 0, front_band[1] + front_well_t, front_depth + front_well_t];
    behind_ctrl = [front_band[0], front_depth + front_well_t, led_at.x + 6, front_depth + front_well_t + 25];   // screen, encoder, button, LED
    behind_usb = [usba_at[0].x - 17, front_depth + front_well_t, usba_at[0].x + 17, front_depth + front_well_t + 25];
    for (f = [["the Pi stack", pi_fp, stack_top + foam_t], ["the front band", band_back, floor_t + front_band[3] + front_well_t + front_depth],
              ["the controls behind the front band", behind_ctrl, floor_t + front_cl + 11],
              ["the USB extensions", behind_usb, floor_t + usba_at[0].y + 8],
              ["the UPS", box2(ups_at, ups_size), stack_z0 + ups_h]])
        if (overlap(gps2, f[1]) && gps_bottom < f[2]) echo(str("WARNING: GPS cradle collides with ", f[0]));
    for (f = [["Wi-Fi adapter", wifi, floor_t + wifi_box.z + 1], ["plug room", plug, stack_z0 + ups_h + pi_h]])
        if (overlap(ants[3][1], f[1]) && gps_bottom < f[2]) echo(str("WARNING: GPS cradle collides with ", f[0]));
    // stack height and panels
    top = stack_top + foam_t;
    if (top > outer.z - lip_h) echo(str("WARNING: stack too tall by ", top - (outer.z - lip_h), " mm"));
    proud = max(2.4 + 8 - front_depth, 3.6 - front_depth, sdr_whip_offset + sdr_whip_d / 2 - rear_depth, 11 - rear_depth, lora_ext ? 11 - sma_well[1] : 0,
                sdr_whip_offset + min(sdr_whip_d / 2 + 0.2 + 2.4, clip_top) - rear_depth);   // last: the whip clips' crown
    if (proud > bumper_t) echo(str("WARNING: panel parts stand ", proud, " mm proud, more than the ", bumper_t, " mm bumpers"));
    bumper_check();
    d = norm([(wifi_a_at.x + wifi_fpc.x / 2) - (wifi_b_at.x + wifi_fpc.y / 2), (wifi_a_at.y + wifi_fpc.y / 2) - (wifi_b_at.y + wifi_fpc.x / 2)]);
    echo(str("Layout: stack headroom ", outer.z - lip_h - top, " mm · plug room ", port_plug_room, " mm · rear-connector clearance ",
             rear_y - sled[3], " mm · fan-to-cells ", ups[0] - fanb[2], " mm · Wi-Fi antennas ", round(d), " mm apart · bumpers ",
             bumper_t, " mm, panel parts stand out up to ", proud, " mm"));
}

module panel_check() {
    function span(x, half) = [x - half, x + half];
    boss_x = [corner_r - 1, outer.x - corner_r + 1];   // the front/rear-middle bosses ride above the band and the channel
    rboss_x = [corner_r - 1, outer.x - corner_r + 1];   // the rear-middle boss rides above the whip channel
    front = [["front band", [front_band[0] - front_well_t, front_band[1] + front_well_t]]];
    // even spacing along the band: [centre, half-width] of each part, left to right
    parts = [[oled_at.x, 18], [encoder_at.x, 7], [button_at.x, 8], [led_at.x, 4], [usba_at[0].x, 17]];
    gaps = concat([parts[0][0] - parts[0][1] - front_band[0]],
                  [for (i = [0:len(parts) - 2]) (parts[i + 1][0] - parts[i + 1][1]) - (parts[i][0] + parts[i][1])],
                  [front_band[1] - parts[len(parts) - 1][0] - parts[len(parts) - 1][1]]);
    if (max(gaps) - min(gaps) > 0.5) echo(str("WARNING: front parts are unevenly spaced: gaps ", gaps));
    for (p = [oled_at, encoder_at, button_at, led_at]) if (p.y != front_cl) echo("WARNING: a front part is off the centre line");
    if ((usba_at[0].y + usba_at[1].y) / 2 != front_cl) echo("WARNING: the USB stack is off the centre line");
    echo(str("Front: centre line ", front_cl, " mm, gaps ", gaps, " mm"));
    rear = concat([["rear band", [rear_band[0] - rear_well_t, rear_band[1] + rear_well_t]]],
                  lora_ext ? [["lora well", span(sma_at[1].x, sma_well[0] / 2 + rear_well_t)]] : []);
    for (c = front, b = boss_x) if (c[1][1] > b - boss_d / 2 && c[1][0] < b + boss_d / 2) echo(str("WARNING: front ", c[0], " hits a lid boss"));
    for (c = rear, b = rboss_x) if (c[1][1] > rv(b) - boss_d / 2 && c[1][0] < rv(b) + boss_d / 2) echo(str("WARNING: rear ", c[0], " hits a lid boss"));
    frame_window_check();
    for (u = [0:len(usba_at) - 2]) if (abs(usba_at[u + 1].y - usba_at[u].y) < 18) echo("WARNING: stacked USB-A flanges overlap");
    for (u = usba_at) if (u.y - 7.5 < front_band[2] || u.y + 7.5 > front_band[3]) echo("WARNING: a USB-A flange doesn't fit the band");
}

module front_label(x, z, t) { translate([x, -3, floor_t + z]) rotate([90, 0, 0]) color("Black") linear_extrude(0.6) text(t, size = 3.6, halign = "center", font = "Liberation Sans:style=Bold"); }
module rear_label(x, z, t)  { translate([rv(x), outer.y + 3, floor_t + z]) rotate([90, 0, 180]) color("Black") linear_extrude(0.6) text(t, size = 3.6, halign = "center", font = "Liberation Sans:style=Bold"); }
module front_view() {
    color("Gainsboro") intersection() { base(); translate([-1, -1, 0]) cube([outer.x + 2, wall + 1, outer.z + 1]); }
    front_label(oled_at.x, oled_at.y + oled_window.y / 2 + 6, "SCREEN");
    front_label(encoder_at.x, encoder_at.y - 10, "KNOB");
    front_label(button_at.x, button_at.y + 10, "POWER");
    front_label(led_at.x, led_at.y - 9, "STATUS");
    for (i = [0:len(usba_at) - 1]) front_label(usba_at[i].x, usba_at[i].y + 8, str("USB ", i + 1));
}
module rear_view() {
    color("Gainsboro") intersection() { base(); translate([-1, outer.y - wall, 0]) cube([outer.x + 2, wall + 1, outer.z + 1]); }
    rear_label(usbc_at.x, usbc_at.y + 6, "USB-C POWER IN");
    rear_label(gx16_at.x, gx16_at.y - 14, "SOLAR / DC IN 8-30 V");
    rear_label(rj45_at.x, rj45_at.y + 11, "ETHERNET");
    if (sensor_sock) rear_label(sensor_sock_at.x, sensor_sock_at.y - 11, "SENSORS");
    for (a = concat([sma_at[0]], lora_ext ? [sma_at[1]] : [], sdr2 != "none" ? [[sdr2_jack.x, sdr2_jack.y - 15, "SDR 2"]] : [])) rear_label(a.x, a.y + 8, a[2]);
    for (cx = sdr_clip_x) rear_label(cx, sma_at[0].y + 12, "CLIP");
}

module panel_test() {
    intersection() { base(); translate([-1, -1, 0]) cube([outer.x + 2, wall + 1, outer.z + 1]); }
    translate([0, -outer.y + wall + 12, 0])
        intersection() { base(); translate([-1, outer.y - wall, 0]) cube([outer.x + 2, wall + 1, outer.z + 1]); }
}
module badger_coat(size) {
    scale(size / 120) union() {
        // dark coat: the face below the mantle, with eyes and nose left standing
        difference() {
            import("badger_head.svg");
            intersection() { import("badger_cap.svg"); import("badger_head.svg"); }
            import("badger_features.svg");
        }
        // a thin outline so the pale helmet and ears still read when engraved
        difference() { offset(r = 3.5) import("badger_head.svg"); import("badger_head.svg"); }
    }
}
// ───────────────────────── showcase (visual only — not printed) ─────────────────────────
// Simple stand-ins for the bought parts so renders show the finished Badger.
// Honey Badger colours: yellow PETG body, black PETG lid, black TPU frames and edge bumpers (renders only; print in those filaments).
body_color = "#F2B705"; lid_color = "#1C1C1C";
group = "all";   // showcase only: export one material group for the Blender renders (see tools/render_blender.py)
GROUPS = [["#F2B705", "body"], ["#1C1C1C", "lid"], ["#E8A317", "amber"], ["#E9E4D6", "lettering"], ["#BFC3C7", "metal"],
          ["#D4AF37", "gold"], ["#141414", "black"], ["#1F5FD1", "blue"], ["#050505", "glass"], ["#DDEBFF", "screen"],
          ["#FF9A2E", "led"], ["#2A2A2A", "black"], ["White", "paper"], ["#B87333", "copper"], ["#2F6B3A", "pcb"], ["#0E0E0E", "bumper"]];
function gname(c) = let(m = [for (g = GROUPS) if (g[0] == c) g[1]]) len(m) ? m[0] : "other";
module paint(c) { if (group == "all") color(c) children(); else if (gname(c) == group) children(); }
C_BODY = body_color; C_AMBER = "#E8A317"; C_MANTLE = "#E9E4D6"; C_METAL = "#BFC3C7";
C_GOLD = "#D4AF37"; C_BLACK = "#141414"; C_BLUE = "#1F5FD1";
module at_front(x, z) { translate([x, 0, floor_t + z]) rotate([90, 0, 0]) children(); }        // local +z = outward
module at_rear(x, z)  { translate([outer.x - x, outer.y, floor_t + z]) rotate([-90, 0, 0]) children(); }
module screw_head() { paint(C_METAL) difference() { cylinder(d = 5.5, h = 1.6, $fn = 24); translate([-0.4, -2, 1]) cube([0.8, 4, 1]); } }
module hexnut(d, h) { cylinder(d = d / cos(30), h = h, $fn = 6); }

module oled_lit() {
    at_front(oled_at.x, oled_at.y) translate([0, 0, -3]) {
        paint("#050505") translate([-(oled_window.x + 4) / 2, -(oled_window.y + 4) / 2, 0]) cube([oled_window.x + 4, oled_window.y + 4, 0.8]);
        paint("#DDEBFF") translate([0, 0, 0.81]) linear_extrude(0.05) {
            translate([-14, 3.2]) text("HoneyBadger", size = 2.6, font = "Liberation Sans:style=Bold");
            translate([-14, -0.8]) text("4 connected  77%", size = 2.1, font = "Liberation Sans");
            translate([-14, -4.6]) text("badger.lan", size = 2.1, font = "Liberation Mono");
        }
    }
}
module knob() {
    // low-profile 14 × 8 mm knob with a fingertip dimple, sitting in the control pocket
    at_front(encoder_at.x, encoder_at.y) {
        paint(C_METAL) hexnut(10, 1.6);
        paint(C_BLACK) translate([0, 0, 2.4]) difference() {
            cylinder(d = 14, h = 8, $fn = 48);
            for (a = [0:12:359]) rotate([0, 0, a]) translate([7, 0, 1]) cylinder(d = 1.1, h = 8, $fn = 8);   // knurl
            translate([3, 0, 8 + 3.2]) sphere(r = 4, $fn = 32);                                              // dimple
        }
        paint(C_AMBER) translate([-0.6, -6.2, 10.41]) cube([1.2, 3, 0.2]);                                    // pointer
    }
}
module power_button() {
    at_front(button_at.x, button_at.y) {
        paint(C_METAL) cylinder(d = 16, h = 2, $fn = 48);
        paint(C_BLACK) translate([0, 0, 2]) cylinder(d = 10.5, h = 1.6, $fn = 48);
        paint(C_AMBER) translate([0, 0, 3.61]) linear_extrude(0.1) {                                       // ⏻ symbol
            difference() { circle(d = 6.2, $fn = 32); circle(d = 4.6, $fn = 32); translate([-1.3, 0]) square([2.6, 4]); }
            translate([-0.4, -0.2]) square([0.8, 3.6]);
        }
    }
}
module status_led() {
    at_front(led_at.x, led_at.y) { paint(C_BLACK) cylinder(d = 7.5, h = 1.2, $fn = 32);
        paint("#FF9A2E") translate([0, 0, 1.2]) { cylinder(d = 5, h = 1.5, $fn = 32); translate([0, 0, 1.5]) sphere(d = 5, $fn = 32); } }
}
module usb_a_port(x, z) {
    at_front(x, z) {
        paint(C_METAL) translate([-usba_cut.x / 2 - 1, -usba_cut.y / 2 - 1, -0.5]) difference() {
            cube([usba_cut.x + 2, usba_cut.y + 2, 1]); translate([1.2, 1.2, -1]) cube([usba_cut.x - 0.4, usba_cut.y - 0.4, 3]); }
        paint(C_BLUE) translate([-usba_cut.x / 2 + 1.5, -0.8, -3]) cube([usba_cut.x - 3, 1.8, 3]);
        for (s = [-1, 1]) translate([s * usba_screw / 2, 0, 0]) screw_head();
    }
}
module usb_c_port() {
    at_rear(usbc_at.x, usbc_at.y) {
        paint(C_METAL) difference() {
            hull() for (s = [-1, 1]) translate([s * 3, 0, -0.5]) cylinder(d = 3.6, h = 1, $fn = 24);
            hull() for (s = [-1, 1]) translate([s * 3, 0, -1]) cylinder(d = 2.6, h = 3, $fn = 24);
        }
        paint(C_BLACK) translate([-2.8, -0.35, -2]) cube([5.6, 0.7, 2]);
        for (s = [-1, 1]) translate([s * usbc_screw / 2, 0, 0]) screw_head();
    }
}
module gx16_socket() {
    at_rear(gx16_at.x, gx16_at.y) {
        paint(C_METAL) { hexnut(20, 2); translate([0, 0, 2]) difference() {
            cylinder(d = 15.6, h = 9, $fn = 48); translate([0, 0, 1]) cylinder(d = 12, h = 9, $fn = 48); }
            for (z = [3:1.2:10]) translate([0, 0, z]) cylinder(d = 16.2, h = 0.5, $fn = 48); }              // thread
        paint(C_BLACK) translate([0, 0, 3]) cylinder(d = 12, h = 5, $fn = 48);
        paint(C_GOLD) for (s = [-1, 1]) translate([s * 2.8, 0, 7.9]) cylinder(d = 1.6, h = 1.5, $fn = 12);
    }
}
module rj45_port() {
    at_rear(rj45_at.x, rj45_at.y) {
        paint(C_BLACK) difference() {
            translate([-rj45_cut.x / 2 - 1.5, -rj45_cut.y / 2 - 1.5, -0.5]) cube([rj45_cut.x + 3, rj45_cut.y + 3, 1.5]);
            translate([-6, -4.5, -2]) cube([12, 9, 4]); }
        paint(C_GOLD) for (i = [0:7]) translate([-4.4 + i * 1.25, 2.6, -1.8]) cube([0.5, 1.6, 0.3]);
        for (s = [-1, 1]) translate([s * rj45_screw / 2, 0, 0]) screw_head();
    }
}
module sma_jack(withcap = false) {
    paint(C_GOLD) { hexnut(8, 2); translate([0, 0, 2]) cylinder(d = 6.3, h = 7, $fn = 32); }
    if (withcap) paint(C_BLACK) translate([0, 0, 6]) cylinder(d = 8, h = 5, $fn = 32);
}
module antenna(len, d, tilt = 0, thin = false) {          // knuckle + whip pointing up
    paint(C_GOLD) translate([0, 0, 9]) cylinder(d = 7.5, h = 4, $fn = 32);
    paint(C_BLACK) translate([0, 0, 13]) {
        sphere(d = 9, $fn = 32);
        rotate([-90 + tilt, 0, 0]) {
            if (thin) { paint(C_METAL) cylinder(d1 = 4, d2 = 1.6, h = len, $fn = 16); }
            else { cylinder(d1 = d, d2 = d * 0.8, h = len, $fn = 32); translate([0, 0, len]) sphere(d = d * 0.8, $fn = 32); }
        }
    }
}
module rear_antennas() {
    // SDR whip folded into its channel and clips (+ the optional capped LoRa EXT jack)
    translate([0, -rear_depth, 0]) {
        at_rear(sma_at[0].x, sma_at[0].y) {
            sma_jack();
            paint(C_GOLD) translate([0, 0, 9]) cylinder(d = 7.5, h = 4, $fn = 32);
            paint(C_BLACK) translate([0, 0, sdr_whip_offset]) { sphere(d = sdr_whip_d, $fn = 32);
                rotate([0, -90, 0]) cylinder(d1 = sdr_whip_d, d2 = sdr_whip_d * 0.6, h = sdr_whip_len, $fn = 32); }
        }
        for (cx = sdr_clip_x) at_rear(cx, sma_at[0].y) paint(C_BLACK) sdr_clip();
    }
    if (lora_ext) translate([0, -sma_well[1], 0]) at_rear(sma_at[1].x, sma_at[1].y) sma_jack(withcap = true);
    if (sdr2 == "jack") translate([0, -rear_depth, 0]) at_rear(sdr2_jack.x, sdr2_jack.y) sma_jack(withcap = true);
    if (sensor_sock) translate([0, -rear_depth, 0]) at_rear(sensor_sock_at.x, sensor_sock_at.y) {       // M8 socket with its cap
        paint(C_METAL) { hexnut(sensor_sock_nut * cos(30), 1.6); translate([0, 0, 1.6]) cylinder(d = 8, h = 7, $fn = 32); }
        paint(C_BLACK) translate([0, 0, 5]) cylinder(d = 10, h = 5, $fn = 32);
    }
}
module inside_fan() {
    translate([wall + 1, fan_at.x, floor_t + fan_at.y]) rotate([0, 90, 0]) {
        paint("#1C1C1C") difference() { translate([-fan / 2, -fan / 2, 0]) cube([fan, fan, 10]); translate([0, 0, -1]) cylinder(d = fan - 2, h = 12); }
        paint("#2A2A2A") { cylinder(d = 14, h = 9); for (a = [0:51:359]) rotate([0, 0, a]) translate([4, -2, 3]) rotate([20, 0, 0]) cube([14, 6, 1]); }
    }
}
module qr_sticker() {
    translate([outer.x - 49, 19, outer.z + lid_t - 0.45]) {
        paint("White") cube([28, 28, 0.4]);
        paint(C_BLACK) for (i = [0:20], j = [0:20]) {
            v = rands(0, 1, 1, i * 31 + j * 7)[0];
            finder = (i < 7 && j < 7) || (i > 13 && j < 7) || (i < 7 && j > 13);
            ring = finder && (i % 14 == 0 || i % 14 == 6 || j % 14 == 0 || j % 14 == 6 || (i % 14 >= 2 && i % 14 <= 4 && j % 14 >= 2 && j % 14 <= 4));
            if ((finder && ring) || (!finder && v > 0.55)) translate([1 + i * 1.24, 1 + j * 1.24, 0.4]) cube([1.24, 1.24, 0.05]);
        }
    }
}

module lid_inside() {
    // the lid flipped over, with the internal antennas fitted
    translate([0, outer.y, lid_t]) rotate([180, 0, 0]) {
        paint(lid_color) lid();
        paint("#B87333") { translate([wifi_a_at.x, wifi_a_at.y, -0.4]) cube([wifi_fpc.x, wifi_fpc.y, 0.3]);
                           translate([wifi_b_at.x, wifi_b_at.y, -0.4]) cube([wifi_fpc.y, wifi_fpc.x, 0.3]); }
        paint("#2F6B3A") translate([lora_at.x, lora_at.y, -0.4]) cube([lora_fpc.x, lora_fpc.y, 0.3]);
        paint(C_BLACK) translate([gps_at.x + 2.2, gps_at.y + 2.2, -6 - gps_box.z + 6]) mirror([0, 0, 1]) cube([gps_box.x, gps_box.y, gps_box.z - 0.1]);
    }
}
module badger_dressed() {   // base, lid, artwork and the bought parts, without the bumpers
    paint(C_BODY) base();
    paint(lid_color) translate([0, 0, outer.z]) lid();
    // lid artwork: a printed label by default, or the colour fill of an inlay / engraving
    lz = markings == "none" ? outer.z + lid_t + 0.02 : outer.z + lid_t - logo_depth + 0.05;
    lh = markings == "none" ? 0.12 : logo_depth - 0.1;
    paint(C_AMBER) translate([0, 0, lz]) linear_extrude(lh) lid_logo();
    paint(C_MANTLE) translate([0, 0, lz]) linear_extrude(lh) lid_text();
    translate([0, front_depth, 0]) { oled_lit(); knob(); power_button(); status_led(); }
    translate([0, front_depth, 0]) for (u = usba_at) usb_a_port(u.x, u.y);
    translate([0, -rear_depth, 0]) { usb_c_port(); gx16_socket(); rj45_port(); } rear_antennas(); inside_fan(); qr_sticker();
}
module frame_screw_heads() for (i = [0:5]) {   // the 2 side lid screws are hidden under the top edge bumpers
    translate([screw_pts[i].x, screw_pts[i].y, Hc + bumper_t - bumper_cbore[1] + 0.5]) screw_head();
    translate([screw_pts[i].x, screw_pts[i].y, -bumper_t + bumper_cbore[1] - 0.5]) mirror([0, 0, 1]) screw_head();
}
module showcase() {
    badger_dressed();
    paint("#0E0E0E") { frames(); edge_bumpers(); }
    frame_screw_heads();
}
module exploded() {
    // fit order: the edge bumpers slide into their grooves first, then the frames go on over their tenons
    badger_dressed();
    paint("#0E0E0E") {
        for (i = [0:3]) at_edge(i) translate([22, 22, 0]) edge_bumper();
        translate([0, -48, 0]) frame();
        translate([outer.x, outer.y + 48, 0]) rotate([0, 0, 180]) frame();
    }
}
// cut-away views of how the parts lock together (each part is cut on its own, so the cut faces keep its colour)
module armour_cut() {
    color("Gold") intersection() { base(); children(); }
    color("#3A3A3A") intersection() { translate([0, 0, outer.z]) lid(); children(); }
    color("#8A8F96") intersection() { frames(); children(); }
    color("#222222") intersection() { edge_bumpers(); children(); }
}
module frame_detail() armour_cut() translate([-20, -20, 40]) cube([29, 70, 60]);   // section at x = 9: tenon plate in the slot under the frame
module edge_section() armour_cut() translate([-20, 60, -20]) cube([outer.x + 40, 40, Hc + 40]);   // section at y = 60: profile + tongue in groove

if (part == "base") base();
else if (part == "lid") translate([0, outer.y, lid_t]) rotate([180, 0, 0]) lid();
else if (part == "lid_view") lid();
else if (part == "lid_inlay") translate([0, outer.y, 0]) mirror([0, 1, 0]) lid_inlay();   // matches the face-down lid
else if (part == "gasket") gasket();
else if (part == "sled") sled();
else if (part == "damper") damper();
else if (part == "frame") translate([0, 0, bumper_t]) rotate([90, 0, 0]) frame();   // face down; print 2 (front and rear are the same)
else if (part == "edge_bumper") multmatrix([[0, 0, 1, 0], [0, 1, 0, 0], [-1, 0, 0, bumper_t], [0, 0, 0, 1]]) edge_bumper();   // on the side leg; print 4
else if (part == "exploded") exploded();
else if (part == "frame_detail") frame_detail();
else if (part == "edge_section") edge_section();
else if (part == "panel_test") panel_test();
else if (part == "front_view") front_view();
else if (part == "rear_view") rear_view();
else if (part == "showcase") showcase();
else if (part == "sdr_clip") sdr_clip();
else if (part == "sdr_shelf") translate([0, 0, floor_t + sdr2_z]) mirror([0, 0, 1]) sdr_shelf();   // deck on the bed
else if (part == "lid_inside") lid_inside();
else if (part == "interior") { base(); envelopes(); }   // fit check without the lid
else {
    base();
    envelopes();
    translate([0, 0, outer.z + 25]) color("LightGray", 0.5) lid();
    color("DarkSlateGray", 0.9) { frames(); edge_bumpers(); }
    panel_check();
    layout_check();
    echo(str("Outer size: ", outer.x, " × ", outer.y, " × ", outer.z + lid_t, " mm; with bumpers ",
             outer.x + 2 * bumper_t, " × ", outer.y + 2 * bumper_t, " × ", Hc + 2 * bumper_t, " mm"));
}
