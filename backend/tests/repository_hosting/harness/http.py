"""Shared stock-client HTTP helpers; credentials are synthetic test fixtures."""

import base64

from tests.repository_hosting.harness.git import Git

AUTH = "http.extraHeader=Authorization: Basic " + base64.b64encode(b"git:git_secret").decode()


def clone(remote, path, source):
    source.run("-c", AUTH, "clone", remote, path)
    return Git(path)


def clone_client(remote, path, source, *options):
    source.run("-c", AUTH, "clone", *options, remote, path)
    client = Git(path)
    client.run("config", "http.extraHeader", AUTH.split("=", 1)[1])
    return client
