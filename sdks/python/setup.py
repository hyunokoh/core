"""Minimal setup.py for the zkCEX Python SDK.

No external dependencies on purpose — stdlib only.
"""

from setuptools import setup, find_packages

setup(
    name="zkcex",
    version="1.0.0",
    description="Official minimal Python SDK for zkCEX.",
    long_description=(
        "Stdlib-only client for the zkCEX REST API. Binance-compatible "
        "HMAC signing for /v3/* and /fapi/v1/*. Bearer-token auth for "
        "the rest. See README.md for the five-line example."
    ),
    author="zkCEX",
    license="See LICENSE-THIRD-PARTY in the parent repo.",
    python_requires=">=3.9",
    packages=find_packages(exclude=("examples", "tests")),
    install_requires=[],  # stdlib-only by design
    classifiers=[
        "Intended Audience :: Developers",
        "Programming Language :: Python :: 3",
        "Programming Language :: Python :: 3 :: Only",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: 3.10",
        "Programming Language :: Python :: 3.11",
        "Programming Language :: Python :: 3.12",
        "Topic :: Office/Business :: Financial",
    ],
)
