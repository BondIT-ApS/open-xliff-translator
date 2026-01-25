# 🧱 Contributing to Open XLIFF Translator

Welcome, Master Builder! We're excited that you want to help build our LEGO castle! 🏰

This document provides guidelines for contributing to the Open XLIFF Translator project. Like any good LEGO instruction manual, following these steps will help ensure all the pieces fit together perfectly!

## 🎯 Table of Contents

- [Code of Conduct](#-code-of-conduct)
- [Getting Started](#-getting-started)
- [How Can I Contribute?](#-how-can-i-contribute)
- [Development Workflow](#-development-workflow)
- [Style Guidelines](#-style-guidelines)
- [Testing](#-testing)
- [Pull Request Process](#-pull-request-process)
- [Community](#-community)

## 📋 Code of Conduct

This project and everyone participating in it is governed by our [LEGO Builder's Code of Conduct](CODE_OF_CONDUCT.md). By participating, you are expected to uphold this code. Please report unacceptable behavior to the project maintainers.

## 🚀 Getting Started

### Prerequisites

Before you start building, make sure you have these LEGO pieces (tools) ready:

- **Docker & Docker Compose**: For containerization
- **Python 3.11+**: The foundation of our backend castle
- **Git**: Version control for tracking your building progress
- **A code editor**: VSCode recommended (see `.vscode/` for our setup)

### Setting Up Your Building Station

1. **Fork the Repository** 🍴
   ```bash
   # Click the "Fork" button on GitHub, then clone your fork
   git clone https://github.com/YOUR-USERNAME/open-xliff-translator.git
   cd open-xliff-translator
   ```

2. **Add Upstream Remote** 🔗
   ```bash
   git remote add upstream https://github.com/BondIT-ApS/open-xliff-translator.git
   ```

3. **Start Building Locally** 🏗️
   ```bash
   docker-compose up -d --build
   ```

4. **Verify Your Build** ✅
   - Flask API: http://localhost:5003
   - LibreTranslate API: http://localhost:5002

## 🎨 How Can I Contribute?

### Reporting Broken Bricks (Bugs) 🐛

Found a bug? Help us fix it!

1. **Check existing issues** to avoid duplicates
2. **Use the bug report template** (if available)
3. **Include**:
   - Clear description of the bug
   - Steps to reproduce
   - Expected vs actual behavior
   - Environment details (OS, Docker version, etc.)
   - Screenshots if relevant

### Suggesting New Building Designs (Features) 💡

Have an idea for improvement?

1. **Check existing feature requests** first
2. **Open a new issue** with the `enhancement` label
3. **Describe**:
   - The problem you're solving
   - Your proposed solution
   - Alternative approaches considered
   - Why it fits the LEGO philosophy

### Building New Features (Code Contributions) 🔧

Ready to contribute code? Awesome! Follow the [Development Workflow](#-development-workflow) below.

### Improving the Instruction Manual (Documentation) 📚

Documentation is as important as the code!

- Fix typos or clarify existing docs
- Add examples or use cases
- Translate documentation
- Improve README or other guides

## 🏗️ Development Workflow

### 1. Create a Building Branch

```bash
# Update your main branch
git checkout main
git pull upstream main

# Create a new branch for your work
git checkout -b feature/your-feature-name
# or
git checkout -b fix/your-bug-fix
```

### Branch Naming Convention

- `feature/` - New features
- `fix/` - Bug fixes
- `docs/` - Documentation updates
- `refactor/` - Code refactoring
- `test/` - Adding or updating tests
- `chore/` - Maintenance tasks

### 2. Make Your Changes

- Write clean, readable code
- Follow our [Style Guidelines](#-style-guidelines)
- Add tests for new functionality
- Update documentation as needed

### 3. Test Your Build

```bash
# Run linting
pylint app.py --rcfile=.pylintrc

# Test Docker build
docker-compose up --build

# Manual testing via browser
open http://localhost:5003
```

### 4. Commit Your Changes

```bash
git add .
git commit -m "type: brief description

Longer explanation of what changed and why (if needed)"
```

**Commit Message Format:**
- `feat:` New feature
- `fix:` Bug fix
- `docs:` Documentation changes
- `style:` Code style changes (formatting, etc.)
- `refactor:` Code refactoring
- `test:` Adding or updating tests
- `chore:` Maintenance tasks

### 5. Push and Create Pull Request

```bash
git push origin your-branch-name
```

Then open a Pull Request on GitHub!

## 📐 Style Guidelines

### Python Code Style

- **PEP 8 compliant**: Follow Python style guide
- **Line length**: Max 120 characters (see `.pylintrc`)
- **Docstrings**: Not required for simple functions (as per project config)
- **Security first**: Always use `defusedxml` for XML parsing
- **Type hints**: Encouraged but not required

### Code Organization

- Keep `app.py` focused and modular
- Extract complex logic into separate functions
- Use meaningful variable and function names
- Comment complex algorithms or business logic

### LEGO Philosophy in Code

- **Every piece matters**: Small, focused functions
- **System thinking**: Organized, predictable structure
- **Playful yet professional**: Code should be maintainable AND enjoyable

## 🧪 Testing

Currently, this project doesn't have automated tests, but we welcome contributions!

**Manual Testing Checklist:**

- [ ] Upload XLIFF file successfully
- [ ] Translation preserves placeholders (`%1$s`, `%n`)
- [ ] Download translated file
- [ ] Verify translated XLIFF has `state="needs-review-translation"`
- [ ] Test with malformed XLIFF files (error handling)
- [ ] Docker build succeeds
- [ ] Pylint passes (or has acceptable warnings)

**Adding Automated Tests:**

We'd love contributions that add:
- Unit tests for XLIFF parsing logic
- Integration tests for translation flow
- Security tests for XML parsing
- End-to-end tests

## 🔄 Pull Request Process

### Before Submitting

1. ✅ Ensure your code follows the style guidelines
2. ✅ Run linting: `pylint app.py --rcfile=.pylintrc`
3. ✅ Test Docker build: `docker-compose up --build`
4. ✅ Update documentation if needed
5. ✅ Rebase on latest main if needed

### PR Checklist

- [ ] Clear, descriptive title
- [ ] Detailed description of changes
- [ ] Links to related issues
- [ ] Screenshots/examples if applicable
- [ ] Documentation updated (if needed)
- [ ] Follows LEGO theme and project style

### What Happens Next?

1. **Automated Quality Gates Run** 🤖
   - Linting
   - Security scanning (Bandit, Safety, CodeQL)
   - Docker build test

2. **Code Review** 👀
   - Maintainers will review your PR
   - Address feedback and make changes
   - Discussion and iteration

3. **Approval & Merge** 🎉
   - Once approved, a maintainer will merge
   - Your contribution becomes part of the LEGO castle!

## 🎭 Community

### Getting Help

- 💬 **GitHub Discussions**: Ask questions, share ideas
- 🐛 **Issues**: Report bugs or request features
- 📧 **Maintainers**: Reach out via GitHub

### Recognition

All contributors are recognized in our project! Every brick builder matters! 🏆

## 🌟 Thank You!

Your contributions help make this project better for everyone. Whether you're fixing a typo or adding a major feature, you're helping build our LEGO castle, one brick at a time!

**Happy Building!** 🧱✨

---

*From Denmark with system and playfulness - The BondIT Way* 🇩🇰
