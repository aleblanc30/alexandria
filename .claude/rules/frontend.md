---
paths:
  - "frontend/**/*.{ts,vue}"
---

# Frontend conventions (`frontend/src/`)

Vue 3 **composition API**, Pinia stores, TypeScript.

| Concern | Location |
|---------|----------|
| Routed views | `views/` (registered in `router.ts`) |
| Shared UI | `components/` |
| Stores | `stores/` (Pinia) |
| API access | `api/client.ts` |
| Global styles | `styles/global.css` |

**`api/client.ts` is the single fetch surface.** Base URL and error handling live
there — extend it rather than writing ad-hoc `fetch` calls in a view or store.

## Verify frontend changes

Run both from `frontend/`:

```
npm run test      # vitest run — *.test.ts under src/lib/, src/api/, src/constants/
npm run build     # vue-tsc && vite build — type-checks as well as builds
```

`npm run build` is not a substitute for `npm run test`; it type-checks but runs
no assertions.
