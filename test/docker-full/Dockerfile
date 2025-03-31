FROM continuumio/miniconda3:latest

RUN conda install -c conda-forge "mamba<2"

WORKDIR /tmp
ADD . /tmp
RUN python -m pip install .
