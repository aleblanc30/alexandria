// Flat config (ESLint 9+). The point of this file is `no-explicit-any`: audit
// item M-10 found 14 `any` annotations, all of them on caught errors funnelling
// into notifyError, where `unknown` plus one narrowing in that single helper
// says the same thing without switching type checking off.
//
// eslint-plugin-vue is on `flat/essential`, not `flat/recommended`. The latter
// adds ~880 template-formatting warnings (attribute-per-line, tag newlines)
// that would rewrite every component for no correctness gain; this repo has no
// Prettier config and formats its templates by hand.
import js from '@eslint/js'
import pluginVue from 'eslint-plugin-vue'
import globals from 'globals'
import tseslint from 'typescript-eslint'

export default [
  {
    ignores: ['dist/**', 'node_modules/**', 'src/api/types.gen.ts'],
  },
  js.configs.recommended,
  ...tseslint.configs.recommended,
  ...pluginVue.configs['flat/essential'],
  {
    files: ['**/*.{ts,vue}'],
    languageOptions: {
      globals: { ...globals.browser },
      parserOptions: { parser: tseslint.parser },
    },
    rules: {
      // TypeScript resolves identifiers itself, and no-undef cannot see type-only
      // names; typescript-eslint's own guidance is to leave it to the compiler.
      'no-undef': 'off',
    },
  },
  {
    rules: {
      '@typescript-eslint/no-explicit-any': 'error',
      // Single-word names are what this codebase uses (BrowseView, DocGridCard).
      'vue/multi-word-component-names': 'off',
    },
  },
]
