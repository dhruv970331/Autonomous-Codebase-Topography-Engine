import os


def target():
    pass


if os.name == "nt":
    def alpha():
        target()

    def beta():
        target()
else:
    def alpha():
        target()
