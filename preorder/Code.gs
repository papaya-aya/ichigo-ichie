/**
 * Ichigo Ichie — pop-up preorder web app (Google Apps Script)
 *
 * Setup:
 *   1. Open the target Google Sheet > Extensions > Apps Script.
 *   2. Paste this into Code.gs and the companion file into Index.html.
 *   3. Deploy > New deployment > Web app.
 *        Execute as: Me      Who has access: Anyone
 *   4. Edit the CONFIG block below any time; re-deploy only when you
 *      change the code, not when you flip SHOP_OPEN (see note below).
 *
 * Note: changing a constant here takes effect on the next page load for
 * anyone using the "test" URL, and after "Deploy > Manage deployments >
 * edit > Version: New version" for the live URL.
 */

/* =====================  CONFIG — edit me  ===================== */

// Master switch. false => the page shows a "preorders are closed" screen
// and the server refuses any order that sneaks through.
var SHOP_OPEN = true;

// Pre-orders also close by themselves at this moment, so nobody can order
// after the cut-off if you forget to flip the switch above.
// Months are 0-based: 7 = August. This is read in the SCRIPT's timezone —
// check File > Project settings > Time zone reads America/Los_Angeles.
// Set to null to rely on SHOP_OPEN alone.
var PREORDER_CLOSES_AT = new Date(2026, 7, 26, 23, 59, 59);
var PREORDER_CLOSES_LABEL = 'Wednesday, August 26';

// The pop-up itself.
var POPUP_VENUE = 'Enchanted Popup Market';
var PICKUP_LOCATION = '1250 22nd St, Dogpatch, SF';
var POPUP_DATE = 'Saturday, August 29';
var POPUP_HOURS = '11:00 AM – 3:00 PM';

// Hard cap on total pieces in a single order.
var MAX_PIECES_PER_CUSTOMER = 24;

// Bundle pricing: any SET_SIZE pieces cost SET_PRICE. Applied as many
// times as it fits, with leftovers charged at the per-piece price.
// Set SET_PRICE to 0 to turn bundling off entirely.
var SET_SIZE = 4;
var SET_PRICE = 30;

// Tipping. Presets are percentages of the food subtotal; 0 renders as
// "No tip". Set TIP_PRESETS to [] and ALLOW_CUSTOM_TIP to false to drop
// tipping from the page entirely.
var TIP_PRESETS = [0, 10, 15, 20];
var ALLOW_CUSTOM_TIP = true;
var MAX_CUSTOM_TIP = 200;

// Tab the orders are appended to. Created automatically if missing.
var SHEET_NAME = 'Orders';

// Order numbers count up from here and are stored on the row, so sorting,
// filtering or deleting rows never changes the number a customer was given.
// Starting above 100 keeps them visually distinct from row numbers.
var FIRST_ORDER_NUMBER = 101;

// Cosmetic only.
var CURRENCY = '$';
var SHOP_NAME = 'Ichigo Ichie';
var CONTACT_EMAIL = 'IchigoIchieSweets@gmail.com';
var CONTACT_IG = 'ichigoichie151515';

/* ===================  END CONFIG  =================== */


var HEADERS = [
  'order_number', 'timestamp', 'name', 'phone', 'email', 'pickup',
  'items_json', 'item_count', 'subtotal', 'tip', 'total', 'notes',
  'payment_status', 'no_show'
];

var ORDER_COUNTER_KEY = 'LAST_ORDER_NUMBER';


/**
 * Customers collect any time during the pop-up, so every order carries the
 * same window. Recorded on the row anyway, so a sheet reused across
 * pop-ups can still be filtered by event.
 */
function pickupWindow_() {
  return [POPUP_DATE, POPUP_HOURS].filter(function (p) { return p; }).join(' · ');
}


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
 * HtmlService strips <meta charset> out of the file, and the iframe Google
 * serves the page inside carries no encoding declaration of its own, so a
 * browser left to guess decodes the UTF-8 bytes as MacRoman — the strawberry
 * comes out as "uci" and every dash as ",Ai".
 *
 * Rather than depend on that guess, everything above ASCII is escaped before
 * it is served. Index.html itself stays readable and editable.
 */
function asciiSafe_(html) {
  return html.split(/(<script[\s\S]*?<\/script>)/i).map(function (part) {
    return /^<script/i.test(part) ? escapeForScript_(part)
                                  : escapeForMarkup_(part);
  }).join('');
}


// One escape per UTF-16 unit: an emoji's surrogate pair becomes two escapes,
// which is exactly what a JavaScript string literal expects.
function escapeForScript_(s) {
  return s.replace(/[^\x00-\x7F]/g, function (ch) {
    return '\\u' + ('000' + ch.charCodeAt(0).toString(16)).slice(-4);
  });
}


// One entity per code point: astral characters must stay whole here, because
// HTML ignores an entity that names a lone surrogate.
function escapeForMarkup_(s) {
  return s.replace(/[^\x00-\x7F]/gu, function (ch) {
    return '&#x' + ch.codePointAt(0).toString(16).toUpperCase() + ';';
  });
}


/**
 * True only while both the manual switch and the cut-off allow orders.
 */
function preordersOpen_() {
  if (!SHOP_OPEN) return false;
  if (PREORDER_CLOSES_AT && new Date() > PREORDER_CLOSES_AT) return false;
  return true;
}


/**
 * Everything the page needs before it can render. Called on load, so
 * flipping SHOP_OPEN closes the shop without touching Index.html.
 */
function getShopState() {
  return {
    open: preordersOpen_(),
    venue: POPUP_VENUE,
    location: PICKUP_LOCATION,
    date: POPUP_DATE,
    hours: POPUP_HOURS,
    closesOn: PREORDER_CLOSES_LABEL,
    maxPieces: MAX_PIECES_PER_CUSTOMER,
    setSize: SET_SIZE,
    setPrice: SET_PRICE,
    tipPresets: TIP_PRESETS,
    allowCustomTip: ALLOW_CUSTOM_TIP,
    maxCustomTip: MAX_CUSTOM_TIP,
    currency: CURRENCY,
    contactEmail: CONTACT_EMAIL,
    contactIg: CONTACT_IG
  };
}


/**
 * Records one order and emails the customer.
 *
 * orderObj = {
 *   name, phone, email, notes,
 *   items: [{id, name_en, name_ja, price, qty}, ...],
 *   tip: {mode: 'percent'|'custom', value: n}
 * }
 *
 * Returns {ok:true, orderNumber:n} or {ok:false, error:'CODE'}.
 * The error codes are turned into sentences by the page, so keep them stable.
 */
function submitOrder(orderObj) {
  try {
    if (!preordersOpen_()) {
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

    // Keep only real lines, and recompute the money server-side.
    var items = [];
    var count = 0;
    var raw = o.items || [];
    for (var i = 0; i < raw.length; i++) {
      var qty = Math.floor(Number(raw[i].qty) || 0);
      var price = Number(raw[i].price) || 0;
      if (qty <= 0) continue;
      items.push({
        id: raw[i].id,
        name_en: raw[i].name_en,
        name_ja: raw[i].name_ja,
        price: price,
        qty: qty
      });
      count += qty;
    }

    if (count === 0) {
      return { ok: false, error: 'NO_ITEMS' };
    }
    if (count > MAX_PIECES_PER_CUSTOMER) {
      return { ok: false, error: 'TOO_MANY' };
    }

    // Must match priceOrder() in Index.html, or the customer is shown one
    // number and charged another.
    var price = priceOrder_(items);

    var tip = resolveTip_(o.tip, price.subtotal);
    if (tip === null) {
      return { ok: false, error: 'BAD_TIP' };
    }
    var total = round2_(price.subtotal + tip);
    var pickup = pickupWindow_();

    // Allocating the number and appending the row must not interleave with
    // another submission, or two orders could be handed the same number.
    var lock = LockService.getScriptLock();
    lock.waitLock(20000);
    var orderNumber;
    try {
      var sheet = getOrdersSheet_();
      orderNumber = nextOrderNumber_(sheet);
      sheet.appendRow([
        orderNumber,
        new Date(),
        name,
        phone,
        email,
        pickup,
        JSON.stringify(items),
        count,
        price.subtotal,
        tip,
        total,
        notes,
        'unpaid',
        ''
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
                        tip, total);
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


/* ---------------------  helpers  --------------------- */

/**
 * Applies the "any SET_SIZE for SET_PRICE" bundle. Pieces are sorted most
 * expensive first so the priciest ones fill the fixed-price sets, which is
 * the reading that favours the customer when flavours differ in price.
 *
 * Returns {full, subtotal, sets, savings}.
 */
function priceOrder_(items) {
  var pieces = [];
  for (var i = 0; i < items.length; i++) {
    for (var q = 0; q < items[i].qty; q++) pieces.push(items[i].price);
  }
  pieces.sort(function (a, b) { return b - a; });

  var full = 0;
  for (var p = 0; p < pieces.length; p++) full += pieces[p];

  var subtotal = 0, sets = 0, k = 0;
  if (SET_SIZE > 0 && SET_PRICE > 0) {
    while (k + SET_SIZE <= pieces.length) {
      var groupFull = 0;
      for (var j = 0; j < SET_SIZE; j++) groupFull += pieces[k + j];
      // Never let the "deal" cost more than buying the pieces outright.
      subtotal += Math.min(SET_PRICE, groupFull);
      sets++;
      k += SET_SIZE;
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
function resolveTip_(tipObj, subtotal) {
  var t = tipObj || {};

  if (t.mode === 'custom') {
    if (!ALLOW_CUSTOM_TIP) return null;
    var amount = Number(t.value);
    if (!isFinite(amount) || amount < 0 || amount > MAX_CUSTOM_TIP) return null;
    return round2_(amount);
  }

  var pct = Number(t.value || 0);
  if (TIP_PRESETS.indexOf(pct) === -1) return null;
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
  var sheet = ss.getSheetByName(SHEET_NAME);
  if (!sheet) {
    sheet = ss.insertSheet(SHEET_NAME);
  }
  if (sheet.getLastRow() === 0) {
    sheet.appendRow(HEADERS);
    sheet.setFrozenRows(1);
  }
  return sheet;
}


function sendConfirmation_(orderNumber, name, email, pickup, items, price,
                           tip, total) {
  var lines = [];
  lines.push('Hi ' + name + ',');
  lines.push('');
  lines.push('Thank you for preordering from ' + SHOP_NAME + '. Your order is confirmed.');
  lines.push('');
  lines.push('Order number: #' + orderNumber);
  lines.push('');
  lines.push('YOUR ORDER');
  lines.push('----------------------------------------');
  for (var i = 0; i < items.length; i++) {
    var it = items[i];
    lines.push(
      it.qty + ' x ' + it.name_en +
      '  (' + money_(it.price) + ' each)  ' +
      money_(it.price * it.qty)
    );
  }
  lines.push('----------------------------------------');
  if (price.sets > 0 && price.savings > 0) {
    lines.push('Items: ' + money_(price.full));
    lines.push(price.sets + ' x set of ' + SET_SIZE + ' (any flavour): -' +
               money_(price.savings));
    lines.push('Subtotal: ' + money_(price.subtotal));
  }
  if (tip > 0) {
    if (price.sets === 0 || price.savings === 0) {
      lines.push('Subtotal: ' + money_(price.subtotal));
    }
    lines.push('Tip: ' + money_(tip));
  }
  lines.push('Total: ' + money_(total));
  lines.push('');
  lines.push('PICKUP — any time during the pop-up');
  lines.push(pickup);
  lines.push(POPUP_VENUE + ', ' + PICKUP_LOCATION);
  lines.push('');
  lines.push('Payment due at pickup — Venmo, credit card or cash.');
  lines.push('');
  lines.push('Questions? Email ' + CONTACT_EMAIL + ' or DM us on Instagram @' +
             CONTACT_IG + '.');
  lines.push('');
  lines.push('See you soon,');
  lines.push(SHOP_NAME);

  MailApp.sendEmail({
    to: email,
    subject: SHOP_NAME + ' — Order #' + orderNumber + ' confirmed',
    body: lines.join('\n'),
    name: SHOP_NAME
  });
}


/**
 * Run this from the Apps Script editor before going live (pick it from the
 * function dropdown, press Run, read the Execution log).
 *
 * It proves the timezone is right, rather than trusting the settings label:
 * if "now" and "closes at" print in Pacific time and "accepting orders"
 * says true, the auto-close is wired up correctly.
 *
 * Never called by the web app — safe to leave in place.
 */
function checkSetup() {
  Logger.log('Script timezone:   ' + Session.getScriptTimeZone());
  Logger.log('Now:               ' + new Date());
  Logger.log('Pre-orders close:  ' + PREORDER_CLOSES_AT);
  Logger.log('Accepting orders?  ' + preordersOpen_());
  Logger.log('Writing to sheet:  ' + SpreadsheetApp.getActiveSpreadsheet().getName() +
             ' > ' + SHEET_NAME);
  Logger.log('Confirmations from: ' + Session.getEffectiveUser().getEmail());
}


function money_(n) {
  return CURRENCY + round2_(n).toFixed(2);
}


function round2_(n) {
  return Math.round(Number(n) * 100) / 100;
}


function trimStr_(v) {
  return String(v === null || v === undefined ? '' : v).trim();
}
