# Copyright 2015 The Chromium Authors. All rights reserved.
# Use of this source code is governed by a BSD-style license that can be
# found in the LICENSE file.

import os
import re
from util import build_utils


class ProguardOutputFilter(object):
  """ProGuard outputs boring stuff to stdout (proguard version, jar path, etc)
  as well as interesting stuff (notes, warnings, etc). If stdout is entirely
  boring, this class suppresses the output.
  """

  IGNORE_RE = re.compile(
      r'Pro.*version|Note:|Reading|Preparing|Printing|ProgramClass:|Searching|'
      r'jar \[|\d+ class path entries checked')

  def __init__(self):
    self._last_line_ignored = False
    self._ignore_next_line = False

  def __call__(self, output):
    ret = []
    for line in output.splitlines(True):
      if self._ignore_next_line:
        self._ignore_next_line = False
        continue

      if '***BINARY RUN STATS***' in line:
        self._last_line_ignored = True
        self._ignore_next_line = True
      elif not line.startswith(' '):
        self._last_line_ignored = bool(self.IGNORE_RE.match(line))
      elif 'You should check if you need to specify' in line:
        self._last_line_ignored = True

      if not self._last_line_ignored:
        ret.append(line)
    return ''.join(ret)


class ProguardCmdBuilder(object):
  def __init__(self, proguard_jar):
    assert os.path.exists(proguard_jar)
    self._proguard_jar_path = proguard_jar
    self._mapping = None
    self._libraries = None
    self._injars = None
    self._configs = None
    self._config_exclusions = None
    self._outjar = None
    self._mapping_output = None
    self._verbose = False
    self._min_api = None
    self._tmp_dir = None
    self._disabled_optimizations = []

  def outjar(self, path):
    assert self._outjar is None
    self._outjar = path

  def mapping_output(self, path):
    assert self._mapping_output is None
    self._mapping_output = path

  def mapping(self, path):
    assert self._mapping is None
    assert os.path.exists(path), path
    self._mapping = path

  def tmp_dir(self, path):
    assert self._tmp_dir is None
    self._tmp_dir = path

  def libraryjars(self, paths):
    assert self._libraries is None
    for p in paths:
      assert os.path.exists(p), p
    self._libraries = paths

  def injars(self, paths):
    assert self._injars is None
    for p in paths:
      assert os.path.exists(p), p
    self._injars = paths

  def configs(self, paths):
    assert self._configs is None
    self._configs = paths
    for p in self._configs:
      assert os.path.exists(p), p

  def config_exclusions(self, paths):
    assert self._config_exclusions is None
    self._config_exclusions = paths

  def verbose(self, verbose):
    self._verbose = verbose

  def min_api(self, min_api):
    assert self._min_api is None
    self._min_api = min_api

  def disable_optimizations(self, optimizations):
    self._disabled_optimizations += optimizations

  def build(self):
    assert self._injars is not None
    assert self._outjar is not None
    assert self._configs is not None

    _combined_injars_path = os.path.join(self._tmp_dir, 'injars.jar')
    _combined_libjars_path = os.path.join(self._tmp_dir, 'libjars.jar')
    _combined_proguard_configs_path = os.path.join(self._tmp_dir,
                                                   'includes.txt')

    build_utils.MergeZips(_combined_injars_path, self._injars)
    build_utils.MergeZips(_combined_libjars_path, self._libraries)
    _CombineConfigs(_combined_proguard_configs_path, self.GetConfigs())

    if self._proguard_jar_path.endswith('.jar'):
      cmd = [
          'java', '-jar', self._proguard_jar_path, '-include',
          _combined_proguard_configs_path
      ]
    else:
      cmd = [self._proguard_jar_path, '@' + _combined_proguard_configs_path]

    if self._mapping:
      cmd += ['-applymapping', self._mapping]

    if self._libraries:
      cmd += ['-libraryjars', _combined_libjars_path]

    if self._min_api:
      cmd += [
          '-assumevalues class android.os.Build$VERSION {' +
          ' public static final int SDK_INT return ' + self._min_api +
          '..9999; }'
      ]

    for optimization in self._disabled_optimizations:
      cmd += [ '-optimizations', '!' + optimization ]

    # The output jar must be specified after inputs.
    cmd += [
        '-forceprocessing',
        '-injars',
        _combined_injars_path,
        '-outjars',
        self._outjar,
        '-printseeds',
        self._outjar + '.seeds',
        '-printusage',
        self._outjar + '.usage',
        '-printmapping',
        self._mapping_output,
    ]

    if self._verbose:
      cmd.append('-verbose')

    return cmd

  def GetDepfileDeps(self):
    # The list of inputs that the GN target does not directly know about.
    inputs = self._configs + self._injars
    if self._libraries:
      inputs += self._libraries
    return inputs

  def GetConfigs(self):
    ret = list(self._configs)
    for path in self._config_exclusions:
      ret.remove(path)
    return ret

  def GetInputs(self):
    inputs = self.GetDepfileDeps()
    inputs += [self._proguard_jar_path]
    if self._mapping:
      inputs.append(self._mapping)
    return inputs

  def GetOutputs(self):
    return [
        self._outjar,
        self._outjar + '.flags',
        self._mapping_output,
        self._outjar + '.seeds',
        self._outjar + '.usage',
    ]

  def _WriteFlagsFile(self, cmd, out):
    # Quite useful for auditing proguard flags.
    WriteFlagsFile(self._configs, out)
    out.write('#' * 80 + '\n')
    out.write('# Command-line\n')
    out.write('#' * 80 + '\n')
    out.write('# ' + ' '.join(cmd) + '\n')

  def CheckOutput(self):
    cmd = self.build()

    # There are a couple scenarios (.mapping files and switching from no
    # proguard -> proguard) where GN's copy() target is used on output
    # paths. These create hardlinks, so we explicitly unlink here to avoid
    # updating files with multiple links.
    for path in self.GetOutputs():
      if os.path.exists(path):
        os.unlink(path)

    with open(self._outjar + '.flags', 'w') as out:
      self._WriteFlagsFile(cmd, out)

    # Warning: and Error: are sent to stderr, but messages and Note: are sent
    # to stdout.
    stdout_filter = None
    stderr_filter = None
    if not self._verbose:
      stdout_filter = ProguardOutputFilter()
      stderr_filter = ProguardOutputFilter()
    build_utils.CheckOutput(cmd, print_stdout=True,
                            print_stderr=True,
                            stdout_filter=stdout_filter,
                            stderr_filter=stderr_filter)

    # Proguard will skip writing -printseeds / -printusage / -printmapping if
    # the files would be empty, but ninja needs all outputs to exist.
    open(self._outjar + '.seeds', 'a').close()
    open(self._outjar + '.usage', 'a').close()
    open(self._outjar + '.mapping', 'a').close()


def _CombineConfigs(output_config_path, input_configs):
  # Combine all input_configs into one config file at output_config_path.
  output_string = ''
  for input_config in input_configs:
    with open(input_config) as f_input_config:
      output_string += f_input_config.read()

  with open(output_config_path, "w+") as f_output_config:
    f_output_config.write(output_string)


def WriteFlagsFile(configs, out, exclude_generated=False):
  for config in sorted(configs):
    if exclude_generated and config.endswith('.resources.proguard.txt'):
      continue

    out.write('#' * 80 + '\n')
    out.write('# ' + config + '\n')
    out.write('#' * 80 + '\n')
    with open(config) as config_file:
      contents = config_file.read().rstrip()
    # Remove numbers from generated rule comments to make file more
    # diff'able.
    contents = re.sub(r' #generated:\d+', '', contents)
    out.write(contents)
    out.write('\n\n')
