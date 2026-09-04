/**
 * Ichigo Ichie — pop-up preorder web app (Google Apps Script)
 *
 * Setup:
 *   1. Open the target Google Sheet > Extensions > Apps Script.
 *   2. Paste this into Code.gs and the companion file into Index.html.
 *   3. Deploy > New deployment > Web app.
 *        Execute as: Me      Who has access: Anyone
 *
 * DAY TO DAY YOU SHOULD NOT NEED TO EDIT THIS FILE.
 *
 * The menu and every setting live in two tabs of the spreadsheet, "Menu"
 * and "Settings", which are created and filled in automatically the first
 * time the site is opened. Editing a cell there takes effect on the next
 * page load — no redeploy. The values below are only the starting point
 * used to build those tabs, and the fallback if a cell is left blank.
 *
 * Code changes are different: those need
 * Deploy > Manage deployments > edit > Version: New version.
 */

/* ==============  STARTING VALUES (seed the Settings tab)  ============== */

var DEFAULTS = {
  // false shows a "pre-orders are closed" screen and refuses orders.
  shop_open: true,

  // Pre-orders also close by themselves at this moment, so nobody can
  // order after the cut-off if the switch above is forgotten. Blank the
  // cell in the Settings tab to rely on shop_open alone.
  // Read in the SCRIPT's timezone — Project settings > Time zone.
  preorder_closes_at: new Date(2026, 7, 26, 23, 59, 59),
  preorder_closes_label: 'Wednesday, August 26',

  // The pop-up itself.
  popup_venue: 'Enchanted Popup Market',
  pickup_location: '1250 22nd St, Dogpatch, SF',
  popup_date: 'Saturday, August 29',
  popup_hours: '11:00 AM – 3:00 PM',

  // Hard cap on total pieces in one order.
  max_pieces_per_customer: 24,

  // Any set_size pieces cost set_price, applied as many times as it fits,
  // with leftovers at the per-piece price. set_price 0 turns it off.
  set_size: 4,
  set_price: 30,

  // Percentages of the food subtotal. 0 renders as "No tip".
  // Empty tip_presets plus allow_custom_tip FALSE removes tipping.
  tip_presets: '0, 10, 15, 20',
  allow_custom_tip: true,
  max_custom_tip: 200,

  // Cosmetic.
  currency: '$',
  shop_name: 'Ichigo Ichie',
  contact_email: 'IchigoIchieSweets@gmail.com',
  contact_ig: 'ichigoichie151515'
};

// One line of help per setting, written into the Settings tab so whoever
// edits it can see what each row does without asking.
var SETTING_NOTES = {
  shop_open: 'TRUE or FALSE. FALSE closes pre-orders immediately.',
  preorder_closes_at: 'Pre-orders stop at this date and time. Leave blank for no automatic close.',
  preorder_closes_label: 'How the closing date is written on the page.',
  popup_venue: 'Name of the market or venue.',
  pickup_location: 'Street address shown to customers.',
  popup_date: 'Pop-up date as you want it written, e.g. Saturday, September 13.',
  popup_hours: 'Opening hours as you want them written.',
  max_pieces_per_customer: 'Most pieces one person can order.',
  set_size: 'How many pieces make a set. 4 means "any 4".',
  set_price: 'Price of one set. 0 turns set pricing off.',
  tip_presets: 'Tip buttons, as percentages separated by commas. 0 shows as "No tip".',
  allow_custom_tip: 'TRUE or FALSE. Shows the "Other" tip button.',
  max_custom_tip: 'Largest custom tip accepted.',
  currency: 'Currency symbol.',
  shop_name: 'Used in the confirmation email.',
  contact_email: 'Shown at the bottom of the page.',
  contact_ig: 'Instagram handle, without the @.'
};

// Starting menu. Edit the Menu tab after the first run, not this.
var DEFAULT_MENU = [
  { id: 'original', name_en: 'Original Strawberry Daifuku',
    name_ja: 'いちご大福', price: 8,
    description_en: 'A whole strawberry and white bean paste in hand-pounded mochi.',
    badge: '', active: true },
  { id: 'matcha', name_en: 'Matcha Strawberry Daifuku',
    name_ja: '抹茶いちご大福', price: 8,
    description_en: 'Uji matcha folded into the mochi for a gentle, grassy bitterness.',
    badge: '', active: true },
  { id: 'hojicha', name_en: 'Hojicha Strawberry Daifuku',
    name_ja: 'ほうじ茶いちご大福', price: 8,
    description_en: 'Roasted hojicha in the mochi — toasty and warm against the berry.',
    badge: '', active: true },
  { id: 'ube', name_en: 'Ube Strawberry Daifuku',
    name_ja: '紅芋いちご大福', price: 8,
    description_en: 'Purple yam and strawberry, back only for this weekend.',
    badge: "This weekend's special", active: true }
];

/* ======================  END STARTING VALUES  ====================== */


var ORDERS_SHEET = 'Orders';
var SETTINGS_SHEET = 'Settings';
var MENU_SHEET = 'Menu';

var HEADERS = [
  'order_number', 'timestamp', 'name', 'phone', 'email', 'pickup',
  'items_json', 'item_count', 'subtotal', 'tip', 'total', 'notes',
  'payment_status', 'no_show'
];

var SETTINGS_HEADERS = ['setting', 'value', 'what it does'];
var MENU_HEADERS = ['id', 'name_en', 'name_ja', 'price', 'description_en',
                    'badge', 'active'];

// Order numbers count up from here and are stored on the row, so sorting,
// filtering or deleting rows never changes the number a customer was given.
var FIRST_ORDER_NUMBER = 101;
var ORDER_COUNTER_KEY = 'LAST_ORDER_NUMBER';


/**
 * Serves the single-page order form.
 */
function doGet() {
  var html = asciiSafe_(HtmlService.createHtmlOutputFromFile('Index').getContent());
  return HtmlService.createHtmlOutput(html)
    .setSandboxMode(HtmlService.SandboxMode.IFRAME)
    .setTitle('Ichigo Ichie Preorder')
    .addMetaTag('viewport', 'width=device-width, initial-scale=1');
}


/**
 * Everything the page needs before it can render, read fresh from the
 * spreadsheet on every load — which is why a menu or settings edit shows
 * up without redeploying.
 */
function getShopState() {
  var cfg = config_();
  return {
    open: preordersOpen_(cfg),
    menu: menu_(),
    venue: cfg.popup_venue,
    location: cfg.pickup_location,
    date: cfg.popup_date,
    hours: cfg.popup_hours,
    closesOn: cfg.preorder_closes_label,
    maxPieces: cfg.max_pieces_per_customer,
    setSize: cfg.set_size,
    setPrice: cfg.set_price,
    tipPresets: cfg.tip_presets,
    allowCustomTip: cfg.allow_custom_tip,
    maxCustomTip: cfg.max_custom_tip,
    currency: cfg.currency,
    contactEmail: cfg.contact_email,
    contactIg: cfg.contact_ig
  };
}


/**
 * Records one order and emails the customer.
 *
 * orderObj = {
 *   name, phone, email, notes,
 *   items: [{id, qty}, ...],
 *   tip: {mode: 'percent'|'custom', value: n}
 * }
 *
 * Prices and names are looked up from the Menu tab, never taken from the
 * page, so what is charged is always what the spreadsheet says.
 *
 * Returns {ok:true, orderNumber:n} or {ok:false, error:'CODE'}.
 * The error codes are turned into sentences by the page, so keep them stable.
 */
function submitOrder(orderObj) {
  try {
    var cfg = config_();
    if (!preordersOpen_(cfg)) {
      return { ok: false, error: 'CLOSED' };
    }

    var o = orderObj || {};
    var name = trimStr_(o.name);
    var phone = trimStr_(o.phone);
    var email = trimStr_(o.email);
    var notes = trimStr_(o.notes);

    if (!name || !phone || !email) {
      return { ok: false, error: 'MISSING_FIELDS' };
    }

    var byId = {};
    menu_().forEach(function (m) { byId[m.id] = m; });

    var items = [];
    var count = 0;
    var raw = o.items || [];
    for (var i = 0; i < raw.length; i++) {
      var qty = Math.floor(Number(raw[i].qty) || 0);
      if (qty <= 0) continue;
      var m = byId[trimStr_(raw[i].id)];
      // An unknown id means the menu changed while someone had the page
      // open. Better to send them back than to guess a price.
      if (!m) return { ok: false, error: 'MENU_CHANGED' };
      items.push({ id: m.id, name_en: m.name_en, name_ja: m.name_ja,
                   price: m.price, qty: qty });
      count += qty;
    }

    if (count === 0) {
      return { ok: false, error: 'NO_ITEMS' };
    }
    if (count > cfg.max_pieces_per_customer) {
      return { ok: false, error: 'TOO_MANY' };
    }

    // Must match priceOrder() in Index.html, or the customer is shown one
    // number and charged another.
    var price = priceOrder_(items, cfg);

    var tip = resolveTip_(o.tip, price.subtotal, cfg);
    if (tip === null) {
      return { ok: false, error: 'BAD_TIP' };
    }
    var total = round2_(price.subtotal + tip);
    var pickup = pickupWindow_(cfg);

    // Allocating the number and appending the row must not interleave with
    // another submission, or two orders could be handed the same number.
    var lock = LockService.getScriptLock();
    lock.waitLock(20000);
    var orderNumber;
    try {
      var sheet = getOrdersSheet_();
      orderNumber = nextOrderNumber_(sheet);
      sheet.appendRow([
        orderNumber, new Date(), name, phone, email, pickup,
        JSON.stringify(items), count, price.subtotal, tip, total,
        notes, 'unpaid', ''
      ]);
      // Commit before releasing the lock: the next order reads this row
      // back when working out which number to hand out.
      SpreadsheetApp.flush();
    } finally {
      lock.releaseLock();
    }

    // The order is already safely on the sheet; a bounced confirmation
    // must not turn a good order into a failure for the customer.
    try {
      sendConfirmation_(orderNumber, name, email, pickup, items, price,
                        tip, total, cfg);
    } catch (mailErr) {
      Logger.log('Confirmation email failed for order ' + orderNumber +
                 ': ' + mailErr);
    }

    return { ok: true, orderNumber: orderNumber };

  } catch (err) {
    Logger.log('submitOrder failed: ' + err + '\n' + (err && err.stack));
    return { ok: false, error: 'SERVER' };
  }
}


/**
 * Run this from the Apps Script editor before going live (pick it from the
 * function dropdown, press Run, read the Execution log). It also creates
 * the Menu and Settings tabs, so it is the quickest way to set them up.
 *
 * Never called by the web app — safe to leave in place.
 */
function checkSetup() {
  var cfg = config_();
  var items = menu_();
  Logger.log('Script timezone:    ' + Session.getScriptTimeZone());
  Logger.log('Now:                ' + new Date());
  Logger.log('Pre-orders close:   ' + cfg.preorder_closes_at);
  Logger.log('Accepting orders?   ' + preordersOpen_(cfg));
  Logger.log('Spreadsheet:        ' + SpreadsheetApp.getActiveSpreadsheet().getName());
  Logger.log('Tabs in use:        ' + [ORDERS_SHEET, MENU_SHEET, SETTINGS_SHEET].join(', '));
  Logger.log('Pop-up:             ' + pickupWindow_(cfg));
  Logger.log('Where:              ' + cfg.popup_venue + ', ' + cfg.pickup_location);
  Logger.log('Menu items on sale: ' + items.length);
  items.forEach(function (m) {
    Logger.log('  - ' + m.name_en + '  ' + cfg.currency + m.price.toFixed(2));
  });
  Logger.log('Confirmations from: ' + Session.getEffectiveUser().getEmail());
}


/* ---------------------  settings and menu  --------------------- */

/**
 * Settings tab merged over DEFAULTS. A blank cell falls back to the
 * default, so clearing a value can never leave the site without one.
 */
function config_() {
  ensureConfigSheets_();

  var raw = {};
  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(SETTINGS_SHEET);
  if (sheet && sheet.getLastRow() > 1) {
    var rows = sheet.getRange(2, 1, sheet.getLastRow() - 1, 2).getValues();
    for (var i = 0; i < rows.length; i++) {
      var key = trimStr_(rows[i][0]).toLowerCase();
      if (key) raw[key] = rows[i][1];
    }
  }

  return {
    shop_open:               bool_(raw.shop_open, DEFAULTS.shop_open),
    // Blank here deliberately means "no automatic close", so it is the one
    // setting that does not fall back to its default.
    preorder_closes_at:      ('preorder_closes_at' in raw)
                               ? date_(raw.preorder_closes_at)
                               : DEFAULTS.preorder_closes_at,
    preorder_closes_label:   str_(raw.preorder_closes_label, DEFAULTS.preorder_closes_label),
    popup_venue:             str_(raw.popup_venue, DEFAULTS.popup_venue),
    pickup_location:         str_(raw.pickup_location, DEFAULTS.pickup_location),
    popup_date:              str_(raw.popup_date, DEFAULTS.popup_date),
    popup_hours:             str_(raw.popup_hours, DEFAULTS.popup_hours),
    max_pieces_per_customer: num_(raw.max_pieces_per_customer, DEFAULTS.max_pieces_per_customer),
    set_size:                num_(raw.set_size, DEFAULTS.set_size),
    set_price:               num_(raw.set_price, DEFAULTS.set_price),
    tip_presets:             list_(raw.tip_presets, DEFAULTS.tip_presets),
    allow_custom_tip:        bool_(raw.allow_custom_tip, DEFAULTS.allow_custom_tip),
    max_custom_tip:          num_(raw.max_custom_tip, DEFAULTS.max_custom_tip),
    currency:                str_(raw.currency, DEFAULTS.currency),
    shop_name:               str_(raw.shop_name, DEFAULTS.shop_name),
    contact_email:           str_(raw.contact_email, DEFAULTS.contact_email),
    contact_ig:              str_(raw.contact_ig, DEFAULTS.contact_ig)
  };
}


/**
 * Menu tab, in sheet order, skipping rows whose active column is not TRUE
 * and rows without an id or a price.
 */
function menu_() {
  ensureConfigSheets_();

  var sheet = SpreadsheetApp.getActiveSpreadsheet().getSheetByName(MENU_SHEET);
  if (!sheet || sheet.getLastRow() < 2) return [];

  var rows = sheet.getRange(2, 1, sheet.getLastRow() - 1,
                            MENU_HEADERS.length).getValues();
  var out = [];
  for (var i = 0; i < rows.length; i++) {
    var r = rows[i];
    var id = trimStr_(r[0]);
    var price = Number(r[3]);
    if (!id || !isFinite(price) || price < 0) continue;
    if (!bool_(r[6], true)) continue;
    out.push({
      id: id,
      name_en: trimStr_(r[1]) || id,
      name_ja: trimStr_(r[2]),
      price: round2_(price),
      description_en: trimStr_(r[4]),
      badge: trimStr_(r[5])
    });
  }
  return out;
}


/**
 * Builds the Menu and Settings tabs the first time they are needed, so a
 * fresh spreadsheet becomes editable without anyone touching the code.
 * Existing tabs are never rewritten.
 */
function ensureConfigSheets_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();

  if (!ss.getSheetByName(SETTINGS_SHEET)) {
    var s = ss.insertSheet(SETTINGS_SHEET);
    s.appendRow(SETTINGS_HEADERS);
    Object.keys(DEFAULTS).forEach(function (key) {
      s.appendRow([key, DEFAULTS[key], SETTING_NOTES[key] || '']);
    });
    s.setFrozenRows(1);
    s.setColumnWidth(1, 190);
    s.setColumnWidth(2, 260);
    s.setColumnWidth(3, 420);
    s.getRange(1, 1, 1, SETTINGS_HEADERS.length).setFontWeight('bold');
  }

  if (!ss.getSheetByName(MENU_SHEET)) {
    var m = ss.insertSheet(MENU_SHEET);
    m.appendRow(MENU_HEADERS);
    DEFAULT_MENU.forEach(function (it) {
      m.appendRow([it.id, it.name_en, it.name_ja, it.price,
                   it.description_en, it.badge, it.active]);
    });
    m.setFrozenRows(1);
    m.setColumnWidth(2, 240);
    m.setColumnWidth(3, 180);
    m.setColumnWidth(5, 420);
    m.getRange(1, 1, 1, MENU_HEADERS.length).setFontWeight('bold');
  }
}


/* ---------------------  helpers  --------------------- */

function preordersOpen_(cfg) {
  if (!cfg.shop_open) return false;
  if (cfg.preorder_closes_at && new Date() > cfg.preorder_closes_at) return false;
  return true;
}


function pickupWindow_(cfg) {
  return [cfg.popup_date, cfg.popup_hours]
    .filter(function (p) { return p; }).join(' · ');
}


/**
 * Applies the "any set_size for set_price" bundle. Pieces are sorted most
 * expensive first so the priciest ones fill the fixed-price sets, which is
 * the reading that favours the customer when flavours differ in price.
 *
 * Returns {full, subtotal, sets, savings}.
 */
function priceOrder_(items, cfg) {
  var pieces = [];
  for (var i = 0; i < items.length; i++) {
    for (var q = 0; q < items[i].qty; q++) pieces.push(items[i].price);
  }
  pieces.sort(function (a, b) { return b - a; });

  var full = 0;
  for (var p = 0; p < pieces.length; p++) full += pieces[p];

  var subtotal = 0, sets = 0, k = 0;
  if (cfg.set_size > 0 && cfg.set_price > 0) {
    while (k + cfg.set_size <= pieces.length) {
      var groupFull = 0;
      for (var j = 0; j < cfg.set_size; j++) groupFull += pieces[k + j];
      // Never let the "deal" cost more than buying the pieces outright.
      subtotal += Math.min(cfg.set_price, groupFull);
      sets++;
      k += cfg.set_size;
    }
  }
  for (; k < pieces.length; k++) subtotal += pieces[k];

  return {
    full: round2_(full),
    subtotal: round2_(subtotal),
    sets: sets,
    savings: round2_(full - subtotal)
  };
}


/**
 * Turns the customer's tip choice into dollars, or null if the choice is
 * one the page could not have produced. Never trust the amount itself —
 * a percentage is recomputed here from the subtotal.
 */
function resolveTip_(tipObj, subtotal, cfg) {
  var t = tipObj || {};

  if (t.mode === 'custom') {
    if (!cfg.allow_custom_tip) return null;
    var amount = Number(t.value);
    if (!isFinite(amount) || amount < 0 || amount > cfg.max_custom_tip) return null;
    return round2_(amount);
  }

  var pct = Number(t.value || 0);
  if (cfg.tip_presets.indexOf(pct) === -1) return null;
  return round2_(subtotal * pct / 100);
}


/**
 * Next order number. Takes the highest of the stored counter and anything
 * already on the sheet, so a number is never reused — not after rows are
 * deleted, and not if the script property is ever cleared.
 *
 * Call only while holding the script lock.
 */
function nextOrderNumber_(sheet) {
  var props = PropertiesService.getScriptProperties();
  var stored = Number(props.getProperty(ORDER_COUNTER_KEY));
  if (!isFinite(stored)) stored = 0;

  var next = Math.max(stored, highestOrderNumberOnSheet_(sheet)) + 1;
  props.setProperty(ORDER_COUNTER_KEY, String(next));
  return next;
}


function highestOrderNumberOnSheet_(sheet) {
  var highest = FIRST_ORDER_NUMBER - 1;
  var lastRow = sheet.getLastRow();
  if (lastRow < 2) return highest;

  var values = sheet.getRange(2, 1, lastRow - 1, 1).getValues();
  for (var i = 0; i < values.length; i++) {
    var n = Number(values[i][0]);
    if (isFinite(n) && n > highest) highest = n;
  }
  return highest;
}


function getOrdersSheet_() {
  var ss = SpreadsheetApp.getActiveSpreadsheet();
  var sheet = ss.getSheetByName(ORDERS_SHEET);
  if (!sheet) {
    sheet = ss.insertSheet(ORDERS_SHEET);
  }
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADERS);
    sheet.setFrozenRows(1);
  }
  return sheet;
}


function sendConfirmation_(orderNumber, name, email, pickup, items, price,
                           tip, total, cfg) {
  var lines = [];
  lines.push('Hi ' + name + ',');
  lines.push('');
  lines.push('Thank you for preordering from ' + cfg.shop_name + '. Your order is confirmed.');
  lines.push('');
  lines.push('Order number: #' + orderNumber);
  lines.push('');
  lines.push('YOUR ORDER');
  lines.push('----------------------------------------');
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    lines.push(
      it.qty + ' x ' + it.name_en +
      '  (' + money_(it.price, cfg) + ' each)  ' +
      money_(it.price * it.qty, cfg)
    );
  }
  lines.push('----------------------------------------');
  if (price.sets > 0 && price.savings > 0) {
    lines.push('Items: ' + money_(price.full, cfg));
    lines.push(price.sets + ' x set of ' + cfg.set_size + ' (any flavour): -' +
               money_(price.savings, cfg));
    lines.push('Subtotal: ' + money_(price.subtotal, cfg));
  }
  if (tip > 0) {
    if (price.sets === 0 || price.savings === 0) {
      lines.push('Subtotal: ' + money_(price.subtotal, cfg));
    }
    lines.push('Tip: ' + money_(tip, cfg));
  }
  lines.push('Total: ' + money_(total, cfg));
  lines.push('');
  lines.push('PICKUP — any time during the pop-up');
  lines.push(pickup);
  lines.push(cfg.popup_venue + ', ' + cfg.pickup_location);
  lines.push('');
  lines.push('Payment due at pickup — Venmo, credit card or cash.');
  lines.push('');
  lines.push('Questions? Email ' + cfg.contact_email + ' or DM us on Instagram @' +
             cfg.contact_ig + '.');
  lines.push('');
  lines.push('See you soon,');
  lines.push(cfg.shop_name);

  MailApp.sendEmail({
    to: email,
    subject: cfg.shop_name + ' — Order #' + orderNumber + ' confirmed',
    body: lines.join('\n'),
    name: cfg.shop_name
  });
}


/* ---------------------  cell readers  --------------------- */

function bool_(v, dflt) {
  if (v === true || v === false) return v;
  var s = trimStr_(v).toLowerCase();
  if (s === '') return dflt;
  if (s === 'true' || s === 'yes' || s === 'y' || s === '1') return true;
  if (s === 'false' || s === 'no' || s === 'n' || s === '0') return false;
  return dflt;
}


function num_(v, dflt) {
  if (v === '' || v === null || v === undefined) return dflt;
  var n = Number(v);
  return isFinite(n) ? n : dflt;
}


function str_(v, dflt) {
  var s = trimStr_(v);
  return s === '' ? dflt : s;
}


/** "0, 10, 15, 20" -> [0, 10, 15, 20]. A single number is allowed too. */
function list_(v, dflt) {
  var source = (v === '' || v === null || v === undefined) ? dflt : v;
  if (typeof source === 'number') return [source];
  var out = [];
  String(source).split(',').forEach(function (part) {
    var n = Number(part.trim());
    if (part.trim() !== '' && isFinite(n)) out.push(n);
  });
  return out;
}


/** A blank cell means "no automatic close", so this may return null. */
function date_(v) {
  if (v instanceof Date) return isFinite(v.getTime()) ? v : null;
  var s = trimStr_(v);
  if (s === '') return null;
  var d = new Date(s);
  return isFinite(d.getTime()) ? d : null;
}


function money_(n, cfg) {
  return cfg.currency + round2_(n).toFixed(2);
}


function round2_(n) {
  return Math.round(Number(n) * 100) / 100;
}


function trimStr_(v) {
  return String(v === null || v === undefined ? '' : v).trim();
}


/* ---------------------  encoding  --------------------- */

/**
 * HtmlService strips <meta charset> and the serving iframe declares no
 * encoding, so browsers guess and mangle any non-ASCII byte. Index.html is
 * written as pure ASCII for that reason; this is the belt to that braces,
 * covering anything non-ASCII that creeps back in.
 */
function asciiSafe_(html) {
  return html.split(/(<script[\s\S]*?<\/script>)/i).map(function (part) {
    return /^<script/i.test(part) ? escapeForScript_(part)
                                  : escapeForMarkup_(part);
  }).join('');
}


function escapeForScript_(s) {
  return s.replace(/[^\x00-\x7F]/g, function (ch) {
    var out = '';
    for (var i = 0; i < ch.length; i++) {
      out += '\\u' + ('000' + ch.charCodeAt(i).toString(16)).slice(-4);
    }
    return out;
  });
}


function escapeForMarkup_(s) {
  return s.replace(/[^\x00-\x7F]/g, function (ch) {
    var out = '';
    for (var i = 0; i < ch.length; i++) {
      out += '&#' + ch.charCodeAt(i) + ';';
    }
    return out;
  });
}
