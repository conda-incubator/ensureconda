FROM golang:latest

COPY main.go /workdir/
COPY go.mod /workdir/
COPY go.sum /workdir/
COPY src/golang/ /workdir/src/golang/
WORKDIR /workdir
RUN go build && mv ensureconda /usr/local/bin
