# Thin loader for [mq_copy] -> copy_mirror singleton
#
# Copyright (C) 2026  Rob Niccum <klober@gmail.com>
#
# This file may be distributed under the terms of the GNU GPLv3 license.

import extras.copy_mirror as copy_mirror


def load_config(config):
    return copy_mirror.load_config(config)
