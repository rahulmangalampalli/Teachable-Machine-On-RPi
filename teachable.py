import argparse
import sys
import os
import time

from abc import abstractmethod
from collections import deque, Counter
from functools import partial

os.environ['XDG_RUNTIME_DIR']='/run/user/1000'

from embedding import KNNEmbeddingEngine
from PIL import Image

import gstreamer

def detectPlatform():
  try:
    model_info = open("/sys/firmware/devicetree/base/model").read()
    if 'Raspberry Pi' in model_info:
      print("Detected Raspberry Pi.")
      return "raspberry"
  except:
    print("Could not detect environment. Assuming generic Linux.")
    return "unknown"


class UI(object):
  """Abstract UI class. Subclassed by specific board implementations."""
  def __init__(self):
    self._button_state = [False for _ in self._buttons]
    current_time = time.time()
    self._button_state_last_change = [current_time for _ in self._buttons]
    self._debounce_interval = 0.1 # seconds

  def setOnlyLED(self, index):
    for i in range(len(self._LEDs)): self.setLED(i, False)
    if index is not None: self.setLED(index, True)

  def isButtonPressed(self, index):
    buttons = self.getButtonState()
    return buttons[index]

  def setLED(self, index, state):
    raise NotImplementedError()

  def getButtonState(self):
    raise NotImplementedError()

  def getDebouncedButtonState(self):
    t = time.time()
    for i,new in enumerate(self.getButtonState()):
      if not new:
        self._button_state[i] = False
        continue
      old = self._button_state[i]
      if ((t-self._button_state_last_change[i]) >
             self._debounce_interval) and not old:
        self._button_state[i] = True
      else:
        self._button_state[i] = False
      self._button_state_last_change[i] = t
    return self._button_state

  def testButtons(self):
    while True:
      for i in range(5):
        self.setLED(i, self.isButtonPressed(i))
      print('Buttons: ', ' '.join([str(i) for i,v in
          enumerate(self.getButtonState()) if v]))
      time.sleep(0.01)

  def wiggleLEDs(self, reps=3):
    for i in range(reps):
      for i in range(5):
        self.setLED(i, True)
        time.sleep(0.05)
        self.setLED(i, False)


class UI_Keyboard(UI):
  def __init__(self):
    global keyinput
    import keyinput

    # Layout of GPIOs for Raspberry demo
    self._buttons = ['q', '1' , '2' , '3', '4']
    self._LEDs = [None]*5
    super(UI_Keyboard, self).__init__()

  def setLED(self, index, state):
    pass

  def getButtonState(self):
    pressed_chars = set()
    while True:
      char = keyinput.get_char()
      if not char : break
      pressed_chars.add(char)

    state = [b in pressed_chars for b in self._buttons]
    return state


class UI_Raspberry(UI):
  def __init__(self):
    # Only for RPi3: set GPIOs to pulldown
    global rpigpio
    import RPi.GPIO as rpigpio
    rpigpio.setmode(rpigpio.BCM)

    # Layout of GPIOs for Raspberry demo
    self._buttons = [16 , 6 , 5 , 24, 27]
    self._LEDs = [20, 13, 12, 25, 22]

    # Initialize them all
    for pin in self._buttons:
      rpigpio.setup(pin, rpigpio.IN, pull_up_down=rpigpio.PUD_DOWN)
    for pin in self._LEDs:
      rpigpio.setwarnings(False)
      rpigpio.setup(pin, rpigpio.OUT)
    super(UI_Raspberry, self).__init__()

  def setLED(self, index, state):
    return rpigpio.output(self._LEDs[index],
           rpigpio.LOW if state else rpigpio.HIGH)

  def getButtonState(self):
    return [rpigpio.input(button) for button in self._buttons]


class TeachableMachine(object):
  """Abstract TeachableMachine class. Subclassed by specific method implementations."""
  @abstractmethod
  def __init__(self, model_path, ui):
    assert os.path.isfile(model_path), 'Model file %s not found'%model_path
    self._ui = ui
    self._start_time = time.time()
    self._frame_times = deque(maxlen=40)

  def visualize(self, classification, svg):
    self._frame_times.append(time.time())
    fps = len(self._frame_times)/float(self._frame_times[-1] - self._frame_times[0] + 0.001)
    # Print/Display results
    self._ui.setOnlyLED(classification)
    classes = ['--', 'One', 'Two', 'Three', 'Four']
    status = 'fps %.1f; #examples: %d; Class % 7s'%(
            fps, self._engine.exampleCount(),
            classes[classification or 0])
    print(status)
    svg.add(svg.text(status, insert=(26, 26), fill='black', font_size='20'))
    svg.add(svg.text(status, insert=(25, 25), fill='white', font_size='20'))

  def classify(self):
    raise NotImplementedError()

class TeachableMachineKNN(TeachableMachine):
  def __init__(self, model_path, ui, KNN=3):
    TeachableMachine.__init__(self, model_path, ui)
    self._buffer = deque(maxlen = 4)
    self._engine = KNNEmbeddingEngine(model_path, KNN)

  def classify(self, img, svg):
    # Classify current image and determine
    emb = self._engine.DetectWithImage(img)
    self._buffer.append(self._engine.kNNEmbedding(emb))
    classification = Counter(self._buffer).most_common(1)[0][0]
    # Interpret user button presses (if any)
    debounced_buttons = self._ui.getDebouncedButtonState()
    for i, b in enumerate(debounced_buttons):
      if not b: continue
      if i == 0: self._engine.clear() # Hitting button 0 resets
      else : self._engine.addEmbedding(emb, i) # otherwise the button # is the class
    # Hitting exactly all 4 class buttons simultaneously quits the program.
    if sum(filter(lambda x:x, debounced_buttons[1:])) == 4 and not debounced_buttons[0]:
      self.clean_shutdown = True
      return True # return True to shut down pipeline
    return self.visualize(classification, svg)


def main(args):
    parser = argparse.ArgumentParser()
    parser.add_argument('--model', help='File path of Tflite model.',
                        default='models/mobilenet_v1_embedding_extractor_cpu.tflite')
    parser.add_argument('--testui', dest='testui', action='store_true',
                        help='Run test of UI. Ctrl-C to abort.')
    parser.add_argument('--keyboard', dest='keyboard', action='store_true',
                        help='Run test of UI. Ctrl-C to abort.')
    args = parser.parse_args()

    # The UI differs a little depending on the system because the GPIOs
    # are a little bit different.
    print('Initialize UI.')
    platform = detectPlatform()
    if args.keyboard:
      ui = UI_Keyboard()
    else:
      if platform == 'raspberry': ui = UI_Raspberry()
      else:
        print('No GPIOs detected - falling back to Keyboard input')
        ui = UI_Keyboard()

    ui.wiggleLEDs()
    if args.testui:
        ui.testButtons()
        return

    print('Initialize Model...')
    teachable = TeachableMachineKNN(args.model, ui)

    print('Start Pipeline.')
    result = gstreamer.run_pipeline(teachable.classify)

    ui.wiggleLEDs(4)


if __name__ == '__main__':
    sys.exit(main(sys.argv))

