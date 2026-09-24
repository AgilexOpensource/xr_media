import os
from glob import glob

from setuptools import find_packages, setup

package_name = "xr_media"

setup(
    name=package_name,
    version="0.1.0",
    packages=find_packages(exclude=["test"]),
    data_files=[
        ("share/ament_index/resource_index/packages", ["resource/" + package_name]),
        ("share/" + package_name, ["package.xml", "README.md", "README.zh-CN.md", "LICENSE"]),
        (os.path.join("share", package_name, "docs"), glob("docs/*.md")),
        (os.path.join("share", package_name, "static"),
         glob("static/*.js") + glob("static/*.css") + glob("static/*.html")),
        (os.path.join("share", package_name, "static", "vendor"), glob("static/vendor/*")),
        (os.path.join("share", package_name, "scripts"), glob("scripts/*.sh")),
    ],
    install_requires=["setuptools", "aiohttp", "aiortc", "av", "numpy"],
    zip_safe=True,
    maintainer="kehuanjack",
    maintainer_email="1506289682@qq.com",
    author="kehuanjack",
    author_email="1506289682@qq.com",
    description="Integrated WebRTC media and WebXR SDK",
    license="Apache-2.0",
    python_requires=">=3.10",
    extras_require={
        "opencv": ["opencv-python-headless>=4.8"],
        "dev": ["pytest>=7"],
    },
    entry_points={
        "console_scripts": ["xr_media = xr_media.cli:main"]
    },
)
