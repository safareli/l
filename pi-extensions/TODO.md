# TODO: Add TypeScript type-checking for pi extensions

## Problem
`@mariozechner/pi-coding-agent` and other pi imports are resolved by pi's runtime (jiti),
but VS Code / TypeScript can't find them — resulting in type errors and no autocomplete.

## Solution
Create a `package.json` here with the pi packages as **devDependencies** and run `npm install`.

### 1. Create `package.json`

```json
{
  "private": true,
  "type": "module",
  "devDependencies": {
    "@mariozechner/pi-coding-agent": "0.51.6",
    "@mariozechner/pi-tui": "0.51.6",
    "@mariozechner/pi-ai": "0.51.6",
    "@sinclair/typebox": "^0.34.0",
    "@typescript/native-preview": "latest"
  }
}
```

### 2. Create `tsconfig.json`

```json
{
  "compilerOptions": {
    "strict": true,
    "module": "ESNext",
    "moduleResolution": "bundler",
    "target": "ESNext",
    "noEmit": true,
    "skipLibCheck": true
  },
  "include": ["*.ts", "**/*.ts"]
}
```

### 3. Run

```bash
npm install
npx tsgo --noEmit  # type-check
```

## Notes
- Match `@mariozechner/*` package versions to your pi version (`pi --version`)
- `devDependencies` only — pi resolves them at runtime from its own bundle
- No build step needed; `tsgo --noEmit` is purely for catching type errors
- If publishing as a pi package, use `peerDependencies` with `"*"` range instead
