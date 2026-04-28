# claude-code-llm-router (DEPRECATED)

⚠️ **This package is deprecated and no longer maintained.**

## 🚀 Migration Required

Please upgrade to [`llm-routing`](https://pypi.org/project/llm-routing/) instead:

```bash
pip install --upgrade llm-routing
```

## What Changed?

The package was renamed from `claude-code-llm-router` to `llm-routing` to better reflect its multi-provider scope (not just Claude).

- **Old package**: `claude-code-llm-router` (this one)
- **New package**: `llm-routing` (recommended)
- **API**: Identical — no code changes needed
- **Features**: All features are in the new package

## Migration Steps

1. **Uninstall old package**:
   ```bash
   pip uninstall claude-code-llm-router
   ```

2. **Install new package**:
   ```bash
   pip install llm-routing
   ```

3. **Update imports** (if using direct imports):
   ```python
   # Old (deprecated)
   from claude_code_llm_router import ...

   # New (recommended)
   from llm_routing import ...
   ```

4. **Or simply use the CLI** (recommended):
   ```bash
   llm-router install
   llm-router stats
   ```

## Why This Package Exists

This package serves as a redirect to help existing users discover the new package. It has a dependency on `llm-routing>=7.6.2`, so installing it will automatically pull in the new package.

## Questions?

- **GitHub Issues**: https://github.com/ypollak2/llm-router/issues
- **GitHub Discussions**: https://github.com/ypollak2/llm-router/discussions
- **Full Documentation**: https://github.com/ypollak2/llm-router

---

**Latest version of llm-routing**: [PyPI](https://pypi.org/project/llm-routing/)
