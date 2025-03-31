FROM continuumio/miniconda3:latest

RUN conda install -c conda-forge mamba

WORKDIR /tmp
ADD . /tmp
RUN python -m pip install .
