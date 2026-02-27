const http = require('http');
const fs = require('fs');
const path = require('path');
const crypto = require('crypto');
const { URL } = require('url');

const PORT = process.env.PORT || 3000;
const ROOT = process.cwd();
const DEFAULT_DB_PATH = path.join(ROOT, 'data', 'db.json');
const RUNTIME_DB_PATH = process.env.RUNTIME_DB_PATH || DEFAULT_DB_PATH;
const FALLBACK_DB_PATH = '/tmp/traceability-db.json';
const AUDIT_HMAC_SECRET = process.env.AUDIT_HMAC_SECRET || 'dev-only-change-me';

const mime = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8'
};

const defaultDb = () => ({
  users: [
    { id: 'u-admin', email: 'admin@example.com', name: 'Admin', role: 'ADMIN' },
    { id: 'u-qa', email: 'qa@example.com', name: 'QA', role: 'QA' },
    { id: 'u-op', email: 'op@example.com', name: 'Operator', role: 'OPERATOR' }
  ],
  products: [], skus: [], lots: [], handlingUnits: [], orders: [], allocations: [], documents: [],
  settings: {
    id: 'org', activePolicyBundleId: 'tier3', allowReshipSealedReturns: false,
    allowSmallToBigOnlyViaRepack: true, requireReasonForExceptions: true,
    requireReleaseForFoodGrade: true, enforceReauthOnSensitiveActions: true
  },
  policyBundles: [
    { id: 'tier1', name: 'Tier1 Basic', rules: {} },
    { id: 'tier2', name: 'Tier2 Controlled', rules: {} },
    { id: 'tier3', name: 'Tier3 QA/Compliance', rules: {} }
  ],
  transactions: [],
  auditLogs: [],
  counters: { product: 1, sku: 1, lot: 1, hu: 1, order: 1, alloc: 1, tx: 1, audit: 1, doc: 1 }
});

function resolveDbPath() {
  if (fs.existsSync(RUNTIME_DB_PATH)) return RUNTIME_DB_PATH;
  try {
    fs.mkdirSync(path.dirname(RUNTIME_DB_PATH), { recursive: true });
    fs.copyFileSync(DEFAULT_DB_PATH, RUNTIME_DB_PATH);
    return RUNTIME_DB_PATH;
  } catch (_) {
    if (!fs.existsSync(FALLBACK_DB_PATH)) {
      fs.copyFileSync(DEFAULT_DB_PATH, FALLBACK_DB_PATH);
    }
    return FALLBACK_DB_PATH;
  }
}

function loadDb() {
  const dbPath = resolveDbPath();
  if (!fs.existsSync(dbPath)) {
    fs.writeFileSync(dbPath, JSON.stringify(defaultDb(), null, 2));
  }
  return JSON.parse(fs.readFileSync(dbPath, 'utf8'));
}

function saveDb(db) {
  const dbPath = resolveDbPath();
  fs.writeFileSync(dbPath, JSON.stringify(db, null, 2));
}

function now() { return new Date().toISOString(); }

function stable(obj) {
  if (Array.isArray(obj)) return obj.map(stable);
  if (obj && typeof obj === 'object') {
    return Object.keys(obj).sort().reduce((a, k) => (a[k] = stable(obj[k]), a), {});
  }
  return obj;
}
function sha256(v) { return crypto.createHash('sha256').update(v).digest('hex'); }
function hmac(v) { return crypto.createHmac('sha256', AUDIT_HMAC_SECRET).update(v).digest('hex'); }

function json(res, code, payload) {
  res.writeHead(code, { 'Content-Type': 'application/json; charset=utf-8' });
  res.end(JSON.stringify(payload));
}

function readBody(req) {
  return new Promise((resolve) => {
    let s = '';
    req.on('data', c => s += c);
    req.on('end', () => resolve(s ? JSON.parse(s) : {}));
  });
}

function nextId(db, key, prefix) {
  const n = db.counters[key]++;
  return `${prefix}${n}`;
}

function actor(req, db) {
  const actorId = req.headers['x-user-id'] || 'u-admin';
  return db.users.find(u => u.id === actorId) || db.users[0];
}


function requireRole(res, user, role) {
  if (user.role !== role) {
    json(res, 403, { error: `${role} role required` });
    return false;
  }
  return true;
}

function canExecute(action, ctx, user, db) {
  const settings = db.settings;
  const bundle = db.policyBundles.find(p => p.id === settings.activePolicyBundleId)?.id || 'tier1';
  const lot = ctx.lot;
  const product = ctx.product;

  const needsRelease = product && (product.requiresRelease || (product.isFoodGrade && settings.requireReleaseForFoodGrade));
  const releaseActions = ['DRAW_FROM_SOURCE', 'CREATE_PACKAGED_UNITS', 'ASSIGN_TO_ORDER', 'REPACK_SPLIT', 'REPACK_COMBINE'];
  if (needsRelease && releaseActions.includes(action) && lot?.status !== 'RELEASED') return `Lot must be RELEASED for ${action}`;

  if (settings.requireReasonForExceptions && ctx.isException && !ctx.reasonCode) return 'Reason code is required for exceptions';

  if (bundle === 'tier3') {
    if ((action === 'RETURN_RELEASE' || (action === 'STATUS_CHANGE' && ctx.toStatus === 'RELEASED')) && !['QA', 'ADMIN'].includes(user.role)) {
      return 'Tier3 requires QA/ADMIN';
    }
    if (action === 'REVERSE' && user.role !== 'ADMIN') return 'Only ADMIN can reverse transactions';
    if (ctx.sensitive && settings.enforceReauthOnSensitiveActions && !ctx.reauth) return 'Re-auth required for sensitive action';
  }
  return null;
}

function appendTx(db, txInput, user) {
  const seq = db.transactions.length ? db.transactions[db.transactions.length - 1].seq + 1 : 1;
  const id = nextId(db, 'tx', 'tx_');
  const createdAt = now();
  const payload = {
    id, seq, createdAt, createdBy: user.id,
    type: txInput.type, reasonCode: txInput.reasonCode || null,
    fromHuId: txInput.fromHuId || null, toHuId: txInput.toHuId || null,
    lotId: txInput.lotId || null, orderId: txInput.orderId || null,
    qtyBase: txInput.qtyBase ?? null, metadata: txInput.metadata || {},
    supersedesTransactionId: txInput.supersedesTransactionId || null
  };
  const prevHash = db.transactions.length ? db.transactions[db.transactions.length - 1].chainHash : null;
  const payloadHash = sha256(JSON.stringify(stable(payload)));
  const chainHash = sha256((prevHash || '') + payloadHash);
  const signature = hmac(chainHash);
  const full = {
    ...payload,
    isException: !!txInput.isException,
    prevHash, payloadHash, chainHash, signature
  };
  db.transactions.push(full);
  return full;
}

function auditLog(db, user, entityType, entityId, action, before, after, reasonCode = null) {
  db.auditLogs.push({ id: nextId(db, 'audit', 'al_'), createdAt: now(), actorId: user.id, entityType, entityId, action, before, after, reasonCode });
}

function toBase(qty, unit, product) {
  const map = {
    mL: 1, L: 1000, 'gal': 3785.41, 'qt': 946.353,
    g: 1, kg: 1000, lb: 453.592,
    ea: 1
  };
  const factor = map[unit] || 1;
  if (product.quantityKind === 'VOLUME' && ['mL', 'L', 'gal', 'qt'].includes(unit)) return qty * factor;
  if (product.quantityKind === 'MASS' && ['g', 'kg', 'lb'].includes(unit)) return qty * factor;
  return qty;
}

function sendFile(res, filePath) {
  if (!fs.existsSync(filePath)) return json(res, 404, { error: 'Not found' });
  const ext = path.extname(filePath);
  res.writeHead(200, { 'Content-Type': mime[ext] || 'text/plain; charset=utf-8' });
  res.end(fs.readFileSync(filePath));
}

function verifyChain(transactions) {
  let prev = null;
  for (const t of transactions.sort((a,b)=>a.seq-b.seq)) {
    const payload = {
      id: t.id, seq: t.seq, createdAt: t.createdAt, createdBy: t.createdBy,
      type: t.type, reasonCode: t.reasonCode || null,
      fromHuId: t.fromHuId || null, toHuId: t.toHuId || null,
      lotId: t.lotId || null, orderId: t.orderId || null, qtyBase: t.qtyBase ?? null,
      metadata: t.metadata || {}, supersedesTransactionId: t.supersedesTransactionId || null
    };
    const payloadHash = sha256(JSON.stringify(stable(payload)));
    const chainHash = sha256((prev || '') + payloadHash);
    const signature = hmac(chainHash);
    if (t.prevHash !== prev || t.payloadHash !== payloadHash || t.chainHash !== chainHash || t.signature !== signature) {
      return { ok: false, brokenSeq: t.seq };
    }
    prev = t.chainHash;
  }
  return { ok: true, brokenSeq: null };
}

function rebuildProjections(db) {
  for (const hu of db.handlingUnits) hu.qtyCurrentBase = hu.qtyOriginalBase;
  for (const hu of db.handlingUnits) hu.integrityState = hu.qtyCurrentBase > 0 ? 'SEALED' : 'EMPTY';
  for (const t of db.transactions.sort((a,b)=>a.seq-b.seq)) {
    if (t.type === 'DRAW_FROM_SOURCE' || t.type === 'REPACK_SPLIT' || t.type === 'REPACK_COMBINE') {
      if (t.fromHuId) {
        const hu = db.handlingUnits.find(h => h.id === t.fromHuId);
        if (hu) {
          hu.qtyCurrentBase = Math.max(0, hu.qtyCurrentBase - (t.qtyBase || 0));
          hu.integrityState = hu.qtyCurrentBase === 0 ? 'EMPTY' : 'PARTIAL';
        }
      }
    }
    if (t.type === 'RETURN_RECEIVE' || t.type === 'CREATE_PACKAGED_UNITS' || t.type === 'REPACK_SPLIT' || t.type === 'REPACK_COMBINE') {
      const outputs = t.metadata?.outputs || [];
      for (const o of outputs) {
        const hu = db.handlingUnits.find(h => h.id === o.huId);
        if (hu) hu.qtyCurrentBase = o.qtyBase;
      }
    }
  }
}

async function handleApi(req, res, urlObj) {
  const db = loadDb();
  const user = actor(req, db);
  const p = urlObj.pathname;

  if (req.method === 'GET' && p === '/api/bootstrap') {
    return json(res, 200, { users: db.users, settings: db.settings, policies: db.policyBundles, products: db.products, skus: db.skus });
  }

  if (req.method === 'GET' && p === '/api/state') return json(res, 200, db);

  if (req.method === 'POST' && p === '/api/products') {
    if (!requireRole(res, user, 'ADMIN')) return;
    const b = await readBody(req);
    const product = { id: nextId(db, 'product', 'p_'), createdAt: now(), createdBy: user.id, updatedAt: now(), updatedBy: user.id, ...b };
    db.products.push(product);
    auditLog(db, user, 'PRODUCT', product.id, 'CREATE', null, product);
    saveDb(db); return json(res, 200, product);
  }

  if (req.method === 'POST' && p === '/api/skus') {
    if (!requireRole(res, user, 'ADMIN')) return;
    const b = await readBody(req);
    const sku = { id: nextId(db, 'sku', 'sku_'), createdAt: now(), createdBy: user.id, updatedAt: now(), updatedBy: user.id, ...b };
    db.skus.push(sku);
    auditLog(db, user, 'SKU', sku.id, 'CREATE', null, sku);
    saveDb(db); return json(res, 200, sku);
  }

  if (req.method === 'POST' && p === '/api/settings') {
    if (!requireRole(res, user, 'ADMIN')) return;
    const b = await readBody(req);
    const before = { ...db.settings };
    db.settings = { ...db.settings, ...b };
    auditLog(db, user, 'SETTINGS', db.settings.id, 'UPDATE', before, db.settings);
    saveDb(db); return json(res, 200, db.settings);
  }


  if (req.method === 'POST' && p === '/api/documents') {
    const b = await readBody(req);
    const doc = {
      id: nextId(db, 'doc', 'doc_'),
      entityType: b.entityType,
      entityId: b.entityId,
      docType: b.docType || 'OTHER',
      filename: b.filename || 'attachment',
      url: b.url || null,
      uploadedAt: now(),
      uploadedBy: user.id
    };
    db.documents.push(doc);
    saveDb(db);
    return json(res, 200, doc);
  }

  if (req.method === 'GET' && p === '/api/documents') {
    const entityType = urlObj.searchParams.get('entityType');
    const entityId = urlObj.searchParams.get('entityId');
    const docs = db.documents.filter(d => (!entityType || d.entityType === entityType) && (!entityId || d.entityId === entityId));
    return json(res, 200, docs);
  }

  if (req.method === 'POST' && p === '/api/receive') {
    const b = await readBody(req);
    const product = db.products.find(x => x.id === b.productId);
    if (!product) return json(res, 400, { error: 'Product not found' });

    const requestedStatus = b.status || 'PENDING';
    const safeStatus = (requestedStatus === 'RELEASED' && !['QA', 'ADMIN'].includes(user.role)) ? 'PENDING' : requestedStatus;
    const lot = {
      id: nextId(db, 'lot', 'lot_'), productId: product.id, supplierName: b.supplierName,
      supplierLot: b.supplierLot, manufacturerLot: b.manufacturerLot || null,
      sourceType: 'PURCHASED', status: safeStatus, receivedAt: now(), expiryDate: b.expiryDate || null,
      notes: b.notes || '', createdAt: now(), createdBy: user.id, updatedAt: now(), updatedBy: user.id
    };
    db.lots.push(lot);

    const qtyBase = toBase(Number(b.qty), b.uom, product);
    const hu = {
      id: nextId(db, 'hu', 'hu_'), huType: b.huType || 'TOTE', barcode: b.barcode,
      productId: product.id, lotId: lot.id, parentHuId: null,
      integrityState: 'SEALED', status: lot.status, qtyOriginalBase: qtyBase, qtyCurrentBase: qtyBase, metadata: {},
      createdAt: now(), createdBy: user.id, updatedAt: now(), updatedBy: user.id
    };
    db.handlingUnits.push(hu);

    const tx = appendTx(db, { type: 'RECEIVE_HU', toHuId: hu.id, lotId: lot.id, qtyBase, metadata: { barcode: hu.barcode, huType: hu.huType } }, user);

    const attachments = Array.isArray(b.attachments) ? b.attachments : [];
    const docs = [];
    for (const a of attachments) {
      const doc = {
        id: nextId(db, 'doc', 'doc_'),
        entityType: a.entityType || 'LOT',
        entityId: a.entityType === 'HU' ? hu.id : lot.id,
        docType: a.docType || 'OTHER',
        filename: a.filename || 'attachment',
        url: a.url || null,
        uploadedAt: now(),
        uploadedBy: user.id
      };
      db.documents.push(doc);
      docs.push(doc);
    }

    saveDb(db);
    return json(res, 200, { lot, hu, tx, documents: docs });
  }

  if (req.method === 'POST' && p === '/api/drawdown') {
    const b = await readBody(req);
    const source = db.handlingUnits.find(h => h.barcode === b.sourceBarcode || h.id === b.sourceBarcode);
    if (!source) return json(res, 400, { error: 'Source HU not found' });
    const lot = db.lots.find(l => l.id === source.lotId);
    const product = db.products.find(p => p.id === source.productId);
    const sku = db.skus.find(s => s.id === b.skuId);
    if (!sku) return json(res, 400, { error: 'SKU not found' });

    const policyErr = canExecute('DRAW_FROM_SOURCE', { lot, product }, user, db);
    if (policyErr) return json(res, 403, { error: policyErr });

    const totalOut = sku.qtyBase * Number(b.count);
    if (source.qtyCurrentBase < totalOut) return json(res, 400, { error: 'Insufficient quantity' });

    source.qtyCurrentBase -= totalOut;
    source.integrityState = source.qtyCurrentBase === 0 ? 'EMPTY' : 'PARTIAL';
    source.updatedAt = now(); source.updatedBy = user.id;

    const drawTx = appendTx(db, { type: 'DRAW_FROM_SOURCE', fromHuId: source.id, lotId: source.lotId, qtyBase: totalOut }, user);
    const outputs = [];
    for (let i = 0; i < Number(b.count); i++) {
      const out = {
        id: nextId(db, 'hu', 'hu_'), huType: b.outputHuType || 'BOTTLE', barcode: `${b.outputPrefix || 'PK'}-${Date.now()}-${i}`,
        productId: source.productId, lotId: source.lotId, parentHuId: null,
        integrityState: 'SEALED', status: lot.status, qtyOriginalBase: sku.qtyBase, qtyCurrentBase: sku.qtyBase, metadata: { skuId: sku.id },
        createdAt: now(), createdBy: user.id, updatedAt: now(), updatedBy: user.id
      };
      db.handlingUnits.push(out);
      outputs.push({ huId: out.id, barcode: out.barcode, qtyBase: out.qtyOriginalBase });
    }
    const createTx = appendTx(db, { type: 'CREATE_PACKAGED_UNITS', fromHuId: source.id, lotId: source.lotId, qtyBase: totalOut, metadata: { outputs } }, user);
    saveDb(db);
    return json(res, 200, { source, drawTx, createTx, outputs });
  }

  if (req.method === 'POST' && p === '/api/assign') {
    const b = await readBody(req);
    const hu = db.handlingUnits.find(h => h.barcode === b.barcode || h.id === b.barcode);
    if (!hu) return json(res, 400, { error: 'HU not found' });
    if (hu.integrityState !== 'SEALED') return json(res, 400, { error: 'Only sealed units can be assigned' });
    const lot = db.lots.find(l => l.id === hu.lotId);
    const product = db.products.find(p => p.id === hu.productId);
    const policyErr = canExecute('ASSIGN_TO_ORDER', { lot, product }, user, db);
    if (policyErr) return json(res, 403, { error: policyErr });

    let order = db.orders.find(o => o.externalOrderNumber === b.externalOrderNumber);
    if (!order) {
      order = { id: nextId(db, 'order', 'ord_'), externalOrderNumber: b.externalOrderNumber, createdAt: now(), createdBy: user.id };
      db.orders.push(order);
    }
    const alloc = { id: nextId(db, 'alloc', 'oa_'), orderId: order.id, huId: hu.id, qtyAllocatedBase: hu.qtyCurrentBase, createdAt: now(), createdBy: user.id };
    db.allocations.push(alloc);
    const tx = appendTx(db, { type: 'ASSIGN_TO_ORDER', toHuId: hu.id, lotId: hu.lotId, orderId: order.id, qtyBase: alloc.qtyAllocatedBase }, user);
    saveDb(db);
    return json(res, 200, { order, alloc, tx });
  }

  if (req.method === 'GET' && p === '/api/lookup') {
    const barcode = urlObj.searchParams.get('barcode');
    const hu = db.handlingUnits.find(h => h.barcode === barcode || h.id === barcode);
    if (!hu) return json(res, 404, { error: 'HU not found' });
    const lot = db.lots.find(l => l.id === hu.lotId);
    const txs = db.transactions.filter(t => t.lotId === hu.lotId || t.fromHuId === hu.id || t.toHuId === hu.id);
    const descendants = [];
    for (const t of txs) for (const o of (t.metadata?.outputs || [])) descendants.push(o);
    const allocs = db.allocations.filter(a => a.huId === hu.id);
    const documents = db.documents.filter(d => (d.entityType === 'HU' && d.entityId === hu.id) || (d.entityType === 'LOT' && d.entityId === lot.id));
    return json(res, 200, { hu, lot, transactions: txs, descendants, allocations: allocs, documents });
  }

  if (req.method === 'POST' && p === '/api/returns/receive') {
    const b = await readBody(req);
    const original = db.handlingUnits.find(h => h.barcode === b.barcode || h.id === b.barcode);
    let productId, lotId;
    if (original) { productId = original.productId; lotId = original.lotId; }
    else { productId = b.productId; lotId = b.lotId; }
    if (!productId || !lotId) return json(res, 400, { error: 'Need known barcode or product+lot' });

    const status = b.condition === 'SEALED' ? 'HOLD' : 'QUARANTINE';
    const qty = Number(b.qtyBase || (original ? original.qtyOriginalBase : 0));
    const hu = {
      id: nextId(db, 'hu', 'hu_'), huType: b.huType || 'BOTTLE', barcode: b.returnBarcode || `RET-${Date.now()}`,
      productId, lotId, parentHuId: null, integrityState: b.condition === 'SEALED' ? 'SEALED' : 'OPENED', status,
      qtyOriginalBase: qty, qtyCurrentBase: qty, metadata: { returnOf: original?.id || null }, createdAt: now(), createdBy: user.id, updatedAt: now(), updatedBy: user.id
    };
    db.handlingUnits.push(hu);
    const tx = appendTx(db, { type: 'RETURN_RECEIVE', isException: true, reasonCode: b.reasonCode, toHuId: hu.id, lotId: hu.lotId, qtyBase: hu.qtyOriginalBase, metadata: { condition: b.condition } }, user);
    saveDb(db);
    return json(res, 200, { hu, tx });
  }

  if (req.method === 'POST' && p === '/api/returns/release') {
    const b = await readBody(req);
    const hu = db.handlingUnits.find(h => h.barcode === b.barcode || h.id === b.huId);
    if (!hu) return json(res, 404, { error: 'Return HU not found' });
    const lot = db.lots.find(l => l.id === hu.lotId);
    const product = db.products.find(p => p.id === hu.productId);
    const policyErr = canExecute('RETURN_RELEASE', { lot, product, sensitive: true, reauth: !!b.reauth }, user, db);
    if (policyErr) return json(res, 403, { error: policyErr });
    if (!db.settings.allowReshipSealedReturns) return json(res, 403, { error: 'Reship sealed returns disabled in settings' });

    hu.status = 'RELEASED'; hu.updatedAt = now(); hu.updatedBy = user.id;
    const tx = appendTx(db, { type: 'RETURN_RELEASE', isException: true, reasonCode: b.reasonCode || 'RETURN_RELEASE', toHuId: hu.id, lotId: hu.lotId, qtyBase: hu.qtyCurrentBase, metadata: { approval: { approvedBy: user.id, approvedAt: now(), reauth: !!b.reauth } } }, user);
    saveDb(db);
    return json(res, 200, { hu, tx });
  }

  if (req.method === 'POST' && p === '/api/repack') {
    const b = await readBody(req);
    const sources = b.sourceBarcodes.map(code => db.handlingUnits.find(h => h.barcode === code || h.id === code)).filter(Boolean);
    if (!sources.length) return json(res, 400, { error: 'No valid source HUs' });
    const first = sources[0];
    if (!sources.every(s => s.productId === first.productId)) return json(res, 400, { error: 'Sources must be same product' });
    if (sources.some(s => s.integrityState === 'OPENED')) return json(res, 400, { error: 'Opened returns cannot be repacked by default' });
    const lotsSet = new Set(sources.map(s => s.lotId));
    const mixedLots = lotsSet.size > 1;

    const lot = db.lots.find(l => l.id === first.lotId);
    const product = db.products.find(p => p.id === first.productId);
    const action = b.mode === 'combine' ? 'REPACK_COMBINE' : 'REPACK_SPLIT';
    const policyErr = canExecute(action, { lot, product, isException: true, reasonCode: b.reasonCode, sensitive: mixedLots, reauth: !!b.reauth }, user, db);
    if (policyErr) return json(res, 403, { error: policyErr });

    const outputSku = db.skus.find(s => s.id === b.outputSkuId);
    if (!outputSku) return json(res, 400, { error: 'Output SKU not found' });

    let available = 0;
    for (const s of sources) available += s.qtyCurrentBase;
    const needed = outputSku.qtyBase * Number(b.count);
    if (available < needed) return json(res, 400, { error: 'Insufficient source quantity' });

    let remainingNeed = needed;
    const consumed = [];
    for (const s of sources) {
      const take = Math.min(s.qtyCurrentBase, remainingNeed);
      s.qtyCurrentBase -= take;
      s.integrityState = s.qtyCurrentBase === 0 ? 'EMPTY' : 'PARTIAL';
      s.updatedAt = now(); s.updatedBy = user.id;
      remainingNeed -= take;
      consumed.push({ huId: s.id, barcode: s.barcode, qtyBase: take });
      if (remainingNeed <= 0) break;
    }

    const outStatus = (!mixedLots && sources.every(s => s.status === 'RELEASED')) ? 'RELEASED' : 'HOLD';
    const outputs = [];
    for (let i=0;i<Number(b.count);i++) {
      const out = {
        id: nextId(db, 'hu', 'hu_'), huType: b.outputHuType || 'BOTTLE', barcode: `RP-${Date.now()}-${i}`,
        productId: first.productId, lotId: first.lotId, parentHuId: null,
        integrityState: 'SEALED', status: outStatus, qtyOriginalBase: outputSku.qtyBase, qtyCurrentBase: outputSku.qtyBase,
        metadata: { skuId: outputSku.id, mixedLots }, createdAt: now(), createdBy: user.id, updatedAt: now(), updatedBy: user.id
      };
      db.handlingUnits.push(out);
      outputs.push({ huId: out.id, barcode: out.barcode, qtyBase: out.qtyOriginalBase });
    }

    const tx = appendTx(db, {
      type: action, isException: true, reasonCode: b.reasonCode,
      fromHuId: consumed[0]?.huId || null, lotId: first.lotId, qtyBase: needed,
      metadata: { sources: consumed, outputs, approval: mixedLots ? { approvedBy: user.id, approvedAt: now(), reauth: !!b.reauth } : null }
    }, user);

    saveDb(db);
    return json(res, 200, { tx, outputs, consumed });
  }

  if (req.method === 'POST' && p === '/api/transactions/reverse') {
    const b = await readBody(req);
    const target = db.transactions.find(t => t.id === b.transactionId);
    if (!target) return json(res, 404, { error: 'Target transaction not found' });
    const policyErr = canExecute('REVERSE', { isException: true, reasonCode: b.reasonCode, sensitive: true, reauth: !!b.reauth }, user, db);
    if (policyErr) return json(res, 403, { error: policyErr });
    const tx = appendTx(db, { type: 'REVERSE', isException: true, reasonCode: b.reasonCode, supersedesTransactionId: target.id, metadata: { note: b.note || '' } }, user);
    saveDb(db);
    return json(res, 200, { tx });
  }

  if (req.method === 'GET' && p === '/api/audit/verify') {
    const result = verifyChain(db.transactions);
    return json(res, 200, { ...result, total: db.transactions.length });
  }

  if (req.method === 'GET' && p === '/api/audit/export') {
    const result = verifyChain(db.transactions);
    return json(res, 200, { verification: result, transactions: db.transactions, auditLogs: db.auditLogs });
  }

  if (req.method === 'POST' && p === '/api/admin/rebuild') {
    if (user.role !== 'ADMIN') return json(res, 403, { error: 'ADMIN only' });
    rebuildProjections(db);
    appendTx(db, { type: 'ADJUST', isException: true, reasonCode: 'REBUILD_PROJECTION', metadata: { action: 'admin/rebuild' } }, user);
    saveDb(db);
    return json(res, 200, { ok: true, handlingUnits: db.handlingUnits.length });
  }

  if (req.method === 'POST' && p === '/api/seed') {
    if (!requireRole(res, user, 'ADMIN')) return;
    db.products = []; db.skus = []; db.lots = []; db.handlingUnits = []; db.orders = []; db.allocations = []; db.transactions = []; db.auditLogs = []; db.counters = defaultDb().counters;
    const admin = db.users[0];
    const acetone = { id: nextId(db, 'product', 'p_'), name: 'Acetone', quantityKind: 'VOLUME', baseUom: 'mL', isFoodGrade: false, requiresRelease: false, createdAt: now(), createdBy: admin.id, updatedAt: now(), updatedBy: admin.id };
    db.products.push(acetone);
    const pint = { id: nextId(db, 'sku', 'sku_'), productId: acetone.id, skuCode: 'AC-PINT', label: 'Pint', qtyBase: toBase(0.5, 'qt', acetone), createdAt: now(), createdBy: admin.id, updatedAt: now(), updatedBy: admin.id };
    const quart = { id: nextId(db, 'sku', 'sku_'), productId: acetone.id, skuCode: 'AC-QT', label: 'Quart', qtyBase: toBase(1, 'qt', acetone), createdAt: now(), createdBy: admin.id, updatedAt: now(), updatedBy: admin.id };
    const gallon = { id: nextId(db, 'sku', 'sku_'), productId: acetone.id, skuCode: 'AC-GAL', label: 'Gallon', qtyBase: toBase(1, 'gal', acetone), createdAt: now(), createdBy: admin.id, updatedAt: now(), updatedBy: admin.id };
    db.skus.push(pint, quart, gallon);
    saveDb(db);
    return json(res, 200, { ok: true });
  }

  return json(res, 404, { error: 'Unknown API route' });
}

async function handleRequest(req, res) {
  const urlObj = new URL(req.url, `http://${req.headers.host || 'localhost'}`);
  if (urlObj.pathname.startsWith('/api/')) return handleApi(req, res, urlObj);

  let file = path.join(ROOT, urlObj.pathname);
  if (urlObj.pathname === '/') file = path.join(ROOT, 'totes', 'receive', 'index.html');
  else if (urlObj.pathname.endsWith('/')) file = path.join(ROOT, urlObj.pathname, 'index.html');
  if (!path.extname(file) && fs.existsSync(file) && fs.statSync(file).isDirectory()) file = path.join(file, 'index.html');
  if (!fs.existsSync(file) && fs.existsSync(`${file}.html`)) file = `${file}.html`;
  if (!fs.existsSync(file)) return json(res, 404, { error: 'Page not found' });
  sendFile(res, file);
}

if (require.main === module) {
  const server = http.createServer((req, res) => {
    handleRequest(req, res).catch((err) => {
      json(res, 500, { error: err.message || 'Unhandled server error' });
    });
  });
  server.listen(PORT, () => console.log(`Server running on http://localhost:${PORT}`));
}

module.exports = { handleRequest };
