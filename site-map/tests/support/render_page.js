// Runs the page's inline script against a minimal DOM stub and prints what
// came out, as JSON, for test_ui_diagram.py to assert on.
//
// `node --check` only proves the script parses. It cannot catch a diagram
// that throws on a payload shape, or lays a box out at NaN - both of which
// are invisible until someone opens the page, which is how the blanked
// dashboard reached production once already.
//
// Usage: node render_page.js <ui dir>
const fs = require("fs");
const path = (process.argv[2] || ".").replace(/\/?$/, "/");
const html = fs.readFileSync(path + "index.html", "utf8");

const els = {};
function el(id) {
  if (!els[id]) els[id] = { id, innerHTML: "", textContent: "", className: "",
                            style: {}, hidden: false, placeholder: "",
                            value: "", focus() {}, addEventListener() {},
                            // A real element has these. Without them the
                            // harness failed on a payload with more than one
                            // site while the page itself was fine - a stub
                            // gap reads exactly like a page bug, so it has
                            // to be faithful about what an element can do.
                            querySelectorAll: () => [],
                            querySelector: () => null,
                            scrollIntoView() {} };
  return els[id];
}
global.window = {};
global.document = {
  getElementById: el,
  addEventListener() {},
  querySelectorAll: () => [],
};
global.localStorage = { getItem: () => null, setItem: () => {} };
global.fetch = () => Promise.reject(new Error("no backend"));

eval(fs.readFileSync(path + "site-data.js", "utf8"));
eval(fs.readFileSync(path + "archive-data.js", "utf8"));

const blocks = [...html.matchAll(/<script(?![^>]*\bsrc=)[^>]*>([\s\S]*?)<\/script>/g)]
  .map(m => m[1]).filter(b => b.trim());
eval(blocks[0]);

const svg = els["dwrap"].innerHTML;
const count = (re) => (svg.match(re) || []).length;
const bad = svg.match(/.{0,80}(NaN|undefined).{0,80}/);

console.log(JSON.stringify({
  svg_chars: svg.length,
  has_svg: svg.includes("<svg"),
  boxes: count(/class="dbox/g),
  proven_edges: count(/dedge proven/g),
  pattern_edges: count(/dedge pattern/g),
  ghost_boxes: count(/dbox ghost/g),
  titles: count(/<title>/g),
  hint: els["diagram-hint"].textContent,
  notes: (els["dnotes"].innerHTML.match(/<li>/g) || []).length,
  site_name: els["site-name"].textContent,
  // The switcher only appears for a payload carrying more than one site.
  site_buttons: (els["site-switch"].innerHTML.match(/<button/g) || []).length,
  anon_boxes: count(/dbox anon/g),
  regions: count(/class="dregion"/g),
  // A region has to actually enclose the boxes it stands for; a placeholder
  // drawn beside them is the confusion it exists to prevent.
  region_encloses: (() => {
    const reg = [...svg.matchAll(/class="dregion"><rect x="([\d.-]+)" y="([\d.-]+)" width="([\d.]+)" height="([\d.]+)"/g)]
      .map(m => ({ x: +m[1], y: +m[2], w: +m[3], h: +m[4] }));
    if (!reg.length) return null;
    const boxes = [...svg.matchAll(/<g class="dbox[^"]*"[^>]*><rect x="([\d.-]+)" y="([\d.-]+)" width="([\d.]+)" height="([\d.]+)"/g)]
      .map(m => ({ x: +m[1], y: +m[2], w: +m[3], h: +m[4] }));
    return reg.map(r => boxes.filter(b =>
      b.x >= r.x && b.y >= r.y &&
      b.x + b.w <= r.x + r.w && b.y + b.h <= r.y + r.h).length);
  })(),
  read_edge_ports: count(/class="daddr" x="[\d.]+" y="[\d.]+">lan/g),
  devices_rendered: (els["devs"].innerHTML.match(/data-node=/g) || []).length,
  banner_chars: els["banner"].innerHTML.length,
  bad_coords: bad ? bad[0] : null,
  scope_glyphs: count(/class="scopeglyph/g),
  addr_lines: count(/class="daddr"/g),
  iface_names: count(/class="difname"/g),
  box_heights: (svg.match(/<rect [^>]*height="(\d+)"/g) || [])
    .map(m => Number(m.match(/height="(\d+)"/)[1])),
  // Smallest horizontal gap between an address's estimated right edge and
  // the interface name on the same line. Negative means they overlap - the
  // bug where `100.64.242.104` ran into `tailscale0`.
  min_label_slack: (() => {
    const ADDR_CH = 6.0;     // 10px IBM Plex Mono, per character
    const rows = {};
    const re = /<text class="(daddr|difname)" x="([\d.]+)" y="([\d.]+)">([^<]*)</g;
    let m, slack = Infinity;
    while ((m = re.exec(svg))) {
      const key = m[3];
      rows[key] = rows[key] || {};
      rows[key][m[1]] = { x: Number(m[2]), text: m[4] };
    }
    for (const key of Object.keys(rows)) {
      const a = rows[key].daddr, n = rows[key].difname;
      if (!a || !n) continue;
      slack = Math.min(slack, n.x - (a.x + a.text.length * ADDR_CH));
    }
    return slack === Infinity ? null : Number(slack.toFixed(1));
  })(),
  leaf_rows: (() => {
    // Distinct y values among the bottom-layer boxes.
    const ys = (svg.match(/<rect [^>]*y="([\d.]+)"/g) || [])
      .map(m => Number(m.match(/y="([\d.]+)"/)[1]));
    return new Set(ys).size;
  })(),
}, null, 2));
