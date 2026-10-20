import copy

import pytest
import yaml


@pytest.fixture
def values():
    return copy.deepcopy(yaml.safe_load(open("examples/values.yaml")))
