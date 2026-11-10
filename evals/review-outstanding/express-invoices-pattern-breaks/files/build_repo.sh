#!/usr/bin/env bash
# Builds a small Express API repo with layered architecture, then adds an
# "invoices" feature as 1 unpushed commit + uncommitted/untracked work that
# breaks patterns, architecture, standards, reinvents helpers, and misses a spec item.
# Usage: build_repo.sh <target-dir>
set -eu
T="$1"; mkdir -p "$T"; cd "$T"
REMOTE="$(mktemp -d)/origin.git"; git init -q --bare "$REMOTE"
git init -q -b main .; git config user.email dev@example.com; git config user.name Dev
mkdir -p src/routes src/services src/repositories src/lib src/utils docs/specs test

cat > CLAUDE.md <<'EOF'
# Project conventions

Layering: `routes/` -> `services/` -> `repositories/`. Routes never import repositories
or `src/db.js` directly; all data access goes through a service.

- Use async/await. Do not use `.then()` chains.
- Wrap every route handler in `asyncHandler` from `src/lib/asyncHandler.js`.
- Throw errors from `src/lib/httpErrors.js` (`NotFoundError`, `ValidationError`); the
  central error middleware turns them into responses. Do not build error JSON in routes.
- Money is stored as integer cents. Format with `formatCurrency` from `src/utils/money.js`.
- Validate request bodies with `src/utils/validation.js`.
EOF

cat > src/db.js <<'EOF'
const rows = { customers: [], invoices: [] };
async function query(table, predicate = () => true) { return rows[table].filter(predicate); }
async function insert(table, row) { const r = { id: rows[table].length + 1, ...row }; rows[table].push(r); return r; }
module.exports = { query, insert };
EOF

cat > src/lib/asyncHandler.js <<'EOF'
module.exports = function asyncHandler(fn) {
  return (req, res, next) => Promise.resolve(fn(req, res, next)).catch(next);
};
EOF

cat > src/lib/httpErrors.js <<'EOF'
class HttpError extends Error { constructor(status, message) { super(message); this.status = status; } }
class NotFoundError extends HttpError { constructor(what) { super(404, `${what} not found`); } }
class ValidationError extends HttpError { constructor(message) { super(400, message); } }
module.exports = { HttpError, NotFoundError, ValidationError };
EOF

cat > src/utils/money.js <<'EOF'
function formatCurrency(cents, currency = 'USD') {
  return new Intl.NumberFormat('en-US', { style: 'currency', currency }).format(cents / 100);
}
module.exports = { formatCurrency };
EOF

cat > src/utils/validation.js <<'EOF'
const { ValidationError } = require('../lib/httpErrors');
const EMAIL_RE = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
function isEmail(value) { return typeof value === 'string' && EMAIL_RE.test(value); }
function requireFields(body, fields) {
  const missing = fields.filter((f) => body[f] === undefined || body[f] === '');
  if (missing.length) throw new ValidationError(`Missing fields: ${missing.join(', ')}`);
}
module.exports = { isEmail, requireFields };
EOF

cat > src/repositories/customerRepository.js <<'EOF'
const db = require('../db');
async function findById(id) { const [c] = await db.query('customers', (r) => r.id === id); return c || null; }
async function create(data) { return db.insert('customers', data); }
module.exports = { findById, create };
EOF

cat > src/services/customerService.js <<'EOF'
const customerRepository = require('../repositories/customerRepository');
const { NotFoundError, ValidationError } = require('../lib/httpErrors');
const { isEmail, requireFields } = require('../utils/validation');

async function getCustomer(id) {
  const customer = await customerRepository.findById(id);
  if (!customer) throw new NotFoundError('Customer');
  return customer;
}

async function createCustomer(body) {
  requireFields(body, ['name', 'email']);
  if (!isEmail(body.email)) throw new ValidationError('Invalid email');
  return customerRepository.create({ name: body.name, email: body.email });
}

module.exports = { getCustomer, createCustomer };
EOF

cat > src/routes/customers.js <<'EOF'
const express = require('express');
const asyncHandler = require('../lib/asyncHandler');
const customerService = require('../services/customerService');

const router = express.Router();

router.get('/:id', asyncHandler(async (req, res) => {
  const customer = await customerService.getCustomer(Number(req.params.id));
  res.json(customer);
}));

router.post('/', asyncHandler(async (req, res) => {
  const customer = await customerService.createCustomer(req.body);
  res.status(201).json(customer);
}));

module.exports = router;
EOF

cat > src/app.js <<'EOF'
const express = require('express');
const app = express();
app.use(express.json());
app.use('/customers', require('./routes/customers'));
app.use((err, req, res, next) => res.status(err.status || 500).json({ error: err.message }));
module.exports = app;
EOF

cat > test/customers.test.js <<'EOF'
const customerService = require('../src/services/customerService');
test('rejects invalid email', async () => {
  await expect(customerService.createCustomer({ name: 'A', email: 'nope' })).rejects.toThrow('Invalid email');
});
EOF

cat > package.json <<'EOF'
{ "name": "billing-api", "version": "1.0.0", "scripts": { "test": "jest" },
  "dependencies": { "express": "^4.19.2" }, "devDependencies": { "jest": "^29.7.0" } }
EOF

cat > docs/specs/invoices.md <<'EOF'
# Invoices (BILL-42)

1. `POST /invoices` creates an invoice for an existing customer: body `{ customerId, amountCents, dueDate }`.
   - 400 if fields are missing or amountCents is not a positive integer.
   - 404 if the customer does not exist.
2. `GET /invoices/:id` returns the invoice with a `formattedAmount` (USD) field. 404 if missing.
3. `GET /invoices?customerId=` lists a customer's invoices, newest first, paginated with `?page=` (20 per page).
EOF

git add -A; git commit -qm "Initial layered API with customers"
git remote add origin "$REMOTE"; git push -q -u origin main

# --- Unpushed commit: invoice repository (fine) ---
cat > src/repositories/invoiceRepository.js <<'EOF'
const db = require('../db');
async function findById(id) { const [i] = await db.query('invoices', (r) => r.id === id); return i || null; }
async function findByCustomer(customerId) { return db.query('invoices', (r) => r.customerId === customerId); }
async function create(data) { return db.insert('invoices', data); }
module.exports = { findById, findByCustomer, create };
EOF
git add -A; git commit -qm "BILL-42: add invoice repository"

# --- Uncommitted: route that breaks everything ---
cat > src/routes/invoices.js <<'EOF'
const express = require('express');
const db = require('../db');
const invoiceRepository = require('../repositories/invoiceRepository');

const router = express.Router();

router.post('/', (req, res, next) => {
  const { customerId, amountCents, dueDate, notifyEmail } = req.body;
  if (!customerId || !amountCents || !dueDate) {
    return res.status(400).json({ error: 'customerId, amountCents and dueDate are required' });
  }
  if (notifyEmail && !/^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(notifyEmail)) {
    return res.status(400).json({ error: 'bad email' });
  }
  db.query('customers', (c) => c.id === customerId).then((customers) => {
    if (customers.length === 0) {
      return res.status(404).json({ error: 'Customer not found' });
    }
    return invoiceRepository.create({ customerId, amountCents, dueDate, createdAt: Date.now() }).then((invoice) => {
      const formatted = '$' + (invoice.amountCents / 100).toFixed(2);
      if (notifyEmail) {
        console.log(`Sending invoice ${invoice.id} for ${formatted} to ${notifyEmail}`);
      }
      res.status(201).json({ ...invoice, formattedAmount: formatted });
    });
  }).catch(next);
});

router.get('/:id', (req, res, next) => {
  invoiceRepository.findById(Number(req.params.id)).then((invoice) => {
    if (!invoice) return res.status(404).json({ error: 'Invoice not found' });
    res.json({ ...invoice, formattedAmount: '$' + (invoice.amountCents / 100).toFixed(2) });
  }).catch(next);
});

router.get('/', async (req, res) => {
  const invoices = await invoiceRepository.findByCustomer(Number(req.query.customerId));
  res.json(invoices);
});

module.exports = router;
EOF

# modify app.js (unstaged)
sed -i "s#app.use('/customers', require('./routes/customers'));#app.use('/customers', require('./routes/customers'));\napp.use('/invoices', require('./routes/invoices'));#" src/app.js
echo "built $T"
