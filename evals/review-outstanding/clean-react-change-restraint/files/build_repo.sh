#!/usr/bin/env bash
# React/TS repo where the outstanding change is GOOD: a new FieldYieldCard component that
# mirrors its sibling (FieldMoistureCard), reuses the existing hook and formatter, has a
# matching test, plus a noisy package-lock.json bump. Tests restraint: a good review
# reports few/no real issues and doesn't invent problems. Usage: build_repo.sh <target-dir>
set -eu
T="$1"; mkdir -p "$T"; cd "$T"
REMOTE="$(mktemp -d)/origin.git"; git init -q --bare "$REMOTE"
git init -q -b main .; git config user.email dev@example.com; git config user.name Dev
mkdir -p src/components src/hooks src/lib src/api

cat > CLAUDE.md <<'EOF'
# Conventions
- Components: function components, named exports, Props type named `<Component>Props`.
- Data fetching only via hooks in `src/hooks/` (which call `src/api/`). Components never call `fetch`.
- Format numbers with helpers in `src/lib/format.ts`.
- Each component has a colocated `*.test.tsx` using Testing Library.
EOF

cat > src/api/client.ts <<'EOF'
export async function getJson<T>(path: string): Promise<T> {
  const res = await fetch(`/api${path}`);
  if (!res.ok) throw new Error(`Request failed: ${res.status}`);
  return res.json() as Promise<T>;
}
EOF

cat > src/hooks/useFieldMetrics.ts <<'EOF'
import { useEffect, useState } from 'react';
import { getJson } from '../api/client';

export type FieldMetrics = { fieldId: string; moisturePct: number; yieldBuPerAcre: number };

export function useFieldMetrics(fieldId: string) {
  const [data, setData] = useState<FieldMetrics | null>(null);
  const [error, setError] = useState<Error | null>(null);
  useEffect(() => {
    let cancelled = false;
    getJson<FieldMetrics>(`/fields/${fieldId}/metrics`)
      .then((d) => { if (!cancelled) setData(d); })
      .catch((e: Error) => { if (!cancelled) setError(e); });
    return () => { cancelled = true; };
  }, [fieldId]);
  return { data, error, loading: !data && !error };
}
EOF

cat > src/lib/format.ts <<'EOF'
export function formatPercent(value: number, digits = 1): string {
  return `${value.toFixed(digits)}%`;
}
export function formatYield(buPerAcre: number): string {
  return `${buPerAcre.toFixed(1)} bu/ac`;
}
EOF

cat > src/components/FieldMoistureCard.tsx <<'EOF'
import { useFieldMetrics } from '../hooks/useFieldMetrics';
import { formatPercent } from '../lib/format';

export type FieldMoistureCardProps = { fieldId: string };

export function FieldMoistureCard({ fieldId }: FieldMoistureCardProps) {
  const { data, error, loading } = useFieldMetrics(fieldId);
  if (loading) return <div className="card">Loading…</div>;
  if (error) return <div className="card card--error">Couldn't load moisture</div>;
  return (
    <div className="card">
      <h3>Soil moisture</h3>
      <p className="card__value">{formatPercent(data!.moisturePct)}</p>
    </div>
  );
}
EOF

cat > src/components/FieldMoistureCard.test.tsx <<'EOF'
import { render, screen } from '@testing-library/react';
import { FieldMoistureCard } from './FieldMoistureCard';
import * as hooks from '../hooks/useFieldMetrics';

test('shows moisture', () => {
  jest.spyOn(hooks, 'useFieldMetrics').mockReturnValue({
    data: { fieldId: 'f1', moisturePct: 23.45, yieldBuPerAcre: 180 }, error: null, loading: false,
  });
  render(<FieldMoistureCard fieldId="f1" />);
  expect(screen.getByText('23.4%')).toBeInTheDocument();
});
EOF

cat > package.json <<'EOF'
{ "name": "field-insights", "version": "1.0.0", "dependencies": { "react": "18.3.1" } }
EOF
python3 -c "
import json
lock={'name':'field-insights','lockfileVersion':3,'packages':{'':{'dependencies':{'react':'18.3.1'}},'node_modules/react':{'version':'18.3.1','integrity':'sha512-aaaa'}}}
json.dump(lock,open('package-lock.json','w'),indent=2)"

git add -A; git commit -qm "Moisture card"
git remote add origin "$REMOTE"; git push -q -u origin main

# ---- Unpushed commit: yield card ----
cat > src/components/FieldYieldCard.tsx <<'EOF'
import { useFieldMetrics } from '../hooks/useFieldMetrics';
import { formatYield } from '../lib/format';

export type FieldYieldCardProps = { fieldId: string };

export function FieldYieldCard({ fieldId }: FieldYieldCardProps) {
  const { data, error, loading } = useFieldMetrics(fieldId);
  if (loading) return <div className="card">Loading…</div>;
  if (error) return <div className="card card--error">Couldn't load yield</div>;
  return (
    <div className="card">
      <h3>Yield</h3>
      <p className="card__value">{formatYield(data!.yieldBuPerAcre)}</p>
    </div>
  );
}
EOF
git add -A; git commit -qm "Add yield card"

# ---- Uncommitted: test + lockfile bump ----
cat > src/components/FieldYieldCard.test.tsx <<'EOF'
import { render, screen } from '@testing-library/react';
import { FieldYieldCard } from './FieldYieldCard';
import * as hooks from '../hooks/useFieldMetrics';

test('shows yield', () => {
  jest.spyOn(hooks, 'useFieldMetrics').mockReturnValue({
    data: { fieldId: 'f1', moisturePct: 20, yieldBuPerAcre: 182.34 }, error: null, loading: false,
  });
  render(<FieldYieldCard fieldId="f1" />);
  expect(screen.getByText('182.3 bu/ac')).toBeInTheDocument();
});
EOF
python3 -c "
import json
lock=json.load(open('package-lock.json'))
for i in range(40):
    lock['packages'][f'node_modules/dep{i}']={'version':'1.0.%d'%i,'integrity':'sha512-%040d'%i}
json.dump(lock,open('package-lock.json','w'),indent=2)"
echo "built $T"
