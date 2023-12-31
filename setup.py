from setuptools import find_packages, setup

# This project is not intended to be released or used as a Python package. This file only exists for scrapyd-client.
# https://github.com/scrapy/scrapyd-client/blob/v1.1.0/README.rst

setup(
    packages=find_packages(exclude=["tests", "tests.*"]),
    include_package_data=True,
    entry_points={
        "scrapy": [
            "settings = generic_scrapy.settings",
        ],
    },
)
