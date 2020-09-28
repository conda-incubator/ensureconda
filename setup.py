#################### Maintained by Hatch ####################
# This file is auto-generated by hatch. If you'd like to customize this file
# please add your changes near the bottom marked for 'USER OVERRIDES'.
# EVERYTHING ELSE WILL BE OVERWRITTEN by hatch.
#############################################################
from io import open

from setuptools import find_packages, setup

with open("README.md", "r", encoding="utf-8") as f:
    readme = f.read()

REQUIRES = ["click", "requests", "appdirs", "filelock"]

setup(
    name="ensureconda",
    version="1.1.0",
    description="Install and run applications packaged with conda in isolated environments",
    long_description=readme,
    long_description_content_type="text/markdown",
    author="Marius van Niekerk",
    author_email="marius.v.niekerk@gmail.com",
    maintainer="Marius van Niekerk",
    maintainer_email="marius.v.niekerk@gmail.com",
    url="https://github.com/conda-incubator/ensureconda",
    license="MIT",
    classifiers=[
        "Development Status :: 4 - Beta",
        "Intended Audience :: Developers",
        "License :: OSI Approved :: MIT License",
        "Natural Language :: English",
        "Operating System :: OS Independent",
        "Programming Language :: Python :: 3.6",
        "Programming Language :: Python :: 3.7",
        "Programming Language :: Python :: 3.8",
        "Programming Language :: Python :: 3.9",
        "Programming Language :: Python :: Implementation :: CPython",
        "Programming Language :: Python :: Implementation :: PyPy",
    ],
    python_requires=">=3.6",
    install_requires=REQUIRES,
    tests_require=["coverage", "pytest"],
    packages=find_packages(exclude=("tests", "tests.*")),
    entry_points={"console_scripts": ["ensureconda = ensureconda.cli:ensureconda_cli"]},
    zip_safe=True,
)
