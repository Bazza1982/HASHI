"""
HASHI setup script.

Note: HASHI is not designed to be installed as a traditional Python package.
This setup.py is primarily for development and dependency management.

Recommended installation:
    git clone https://github.com/Bazza1982/hashi.git
    cd hashi
    pip install -r requirements.txt
    python onboarding/onboarding_main.py
"""

from setuptools import setup, find_packages
from pathlib import Path

# Read README for long description
readme_file = Path(__file__).parent / "README.md"
long_description = readme_file.read_text(encoding="utf-8") if readme_file.exists() else ""

# Read requirements
requirements_file = Path(__file__).parent / "requirements.txt"
requirements = []
if requirements_file.exists():
    with open(requirements_file, 'r', encoding='utf-8') as f:
        requirements = [
            line.strip() 
            for line in f 
            if line.strip() and not line.startswith('#')
        ]

setup(
    name="hashi-bridge",
    version="1.0.1",
    description="HASHI — Universal AI Agent Orchestration Platform",
    long_description=long_description,
    long_description_content_type="text/markdown",
    author="HASHI Team",
    author_email="barrytianli@gmail.com",
    url="https://github.com/Bazza1982/hashi",
    project_urls={
        "Documentation": "https://github.com/Bazza1982/hashi#readme",
        "Source": "https://github.com/Bazza1982/hashi",
        "Issues": "https://github.com/Bazza1982/hashi/issues",
    },
    license="MIT",
    keywords=[
        "ai", "agent", "orchestration", "claude", "gemini", "codex",
        "telegram", "whatsapp", "multi-agent", "chatbot", "automation"
    ],
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "Intended Audience :: Science/Research",
        "License :: OSI Approved :: MIT License",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Communications :: Chat",
        "Topic :: Scientific/Engineering :: Artificial Intelligence",
    ],
    python_requires=">=3.10",
    install_requires=requirements,
    extras_require={
        "whatsapp": ["neonize>=1.0.0"],
        "voice": ["edge-tts>=6.0.0"],
    },
    # Don't install as a package - this is an application
    packages=[],
    include_package_data=False,
)
