# Contributing to Manufex

Thank you for your interest in contributing to Manufex! We welcome all forms of contributions.

## Reporting Bugs

Before submitting a bug report, please ensure:

1. Check [Issues](https://github.com/gtt116/enikk/issues) to see if it has already been reported
2. Test with the latest version to confirm the issue still exists

When submitting, please provide:
- OS version (Windows 10/11)
- Steps to reproduce
- Expected vs actual behavior
- Screenshots or logs (if applicable)

## Feature Requests

1. Discuss your idea in [Issues](https://github.com/gtt116/enikk/issues) first
2. Explain the use case and expected outcome
3. Wait for maintainer confirmation before development

## Development Workflow

### 1. Fork and Clone

```bash
git clone https://github.com/YOUR_USERNAME/enikk.git
cd enikk
```

### 2. Create Branch

Create a feature branch from `main`:

```bash
git checkout -b feature/your-feature-name
```

Branch naming conventions:
- Feature: `feature/xxx`
- Bug fix: `fix/xxx`
- Documentation: `docs/xxx`
- Refactoring: `refactor/xxx`

### 3. Setup Development Environment

```bash
# Install dependencies with uv
uv venv --seed
uv pip install -e ".[dev]"

# Verify installation
enikk
```

### 4. Code Standards

- **Formatting**: Project uses ruff for auto-formatting
- **Type hints**: Recommended but not required

Run before committing:

```bash
# Lint and type check
.\lint.bat

# Run tests
.\test.bat
```

### 5. Commit Guidelines

- Use clear commit messages describing the changes
- One commit should address one thing
- Avoid committing debug code or temporary files

### 6. Submit Pull Request

1. Push to your fork repository
2. Submit PR from your branch to `main`
3. In the PR description, include:
   - Purpose and background of the changes
   - Related issue numbers (if any)
   - Testing methods
4. Wait for code review

## Questions?

If you have any questions, feel free to ask in [Issues](https://github.com/gtt116/enikk/issues).
