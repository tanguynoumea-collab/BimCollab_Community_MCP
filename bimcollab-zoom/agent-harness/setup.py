from setuptools import setup, find_namespace_packages

setup(
    name="cli-anything-bczoom",
    version="1.0.0",
    description="Agent-usable CLI for BIMcollab Zoom: smart views, clash rules, lists, "
                "projects and Nightshift automation",
    packages=find_namespace_packages(include=["cli_anything.*"]),
    package_data={"cli_anything.bczoom": ["skills/*.md", "README.md", "tests/TEST.md"]},
    install_requires=["click>=8.0.0", "prompt-toolkit>=3.0.0"],
    extras_require={"test": ["pytest>=7"], "live": ["pywinauto>=0.6.8", "pillow>=10", "pywin32>=306; sys_platform == 'win32'"]},
    entry_points={"console_scripts": [
        "cli-anything-bczoom=cli_anything.bczoom.bczoom_cli:main",
    ]},
    python_requires=">=3.10",
)
