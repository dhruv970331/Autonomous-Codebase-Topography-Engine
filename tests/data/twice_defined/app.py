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


class Box:
    if os.name == "nt":
        def size(self):
            return 1
    else:
        @property
        def size(self):
            return 2


if os.name == "nt":
    class Shape:
        def size(self):
            return 3
else:
    class Shape:
        sides = 0

        def size(self):
            return 4
