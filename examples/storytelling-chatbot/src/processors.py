import re

from async_timeout import timeout
from prompts import CUE_ASSISTANT_TURN, CUE_USER_TURN, IMAGE_GEN_PROMPT
from utils.helpers import load_sounds

from pipecat.frames.frames import (
    Frame,
    LLMFullResponseEndFrame,
    TextFrame,
    UserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.transports.services.daily import DailyTransportMessageFrame

sounds = load_sounds(["talking.wav", "listening.wav", "ding.wav"])

# -------------- Frame Types ------------- #


class StoryPageFrame(TextFrame):
    # Frame for each sentence in the story before a [break]
    pass


class StoryImageFrame(TextFrame):
    # Frame for trigger image generation
    pass


class StoryPromptFrame(TextFrame):
    # Frame for prompting the user for input
    pass


# ------------ Frame Processors ----------- #


class StoryImageProcessor(FrameProcessor):
    """Processor for image prompt frames that will be sent to the FAL service.

    This processor is responsible for consuming frames of type `StoryImageFrame`.
    It processes them by passing it to the FAL service.
    The processed frames are then yielded back.

    Attributes:
        _fal_service (FALService): The FAL service, generates the images (fast fast!).
    """

    def __init__(self, fal_service):
        super().__init__()
        self._fal_service = fal_service

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, StoryImageFrame):
            try:
                async with timeout(7):
                    async for i in self._fal_service.run_image_gen(IMAGE_GEN_PROMPT % frame.text):
                        await self.push_frame(i)
            except TimeoutError:
                pass
            pass
        else:
            await self.push_frame(frame)


class StoryProcessor(FrameProcessor):
    """Primary frame processor. It takes the frames generated by the LLM
    and processes them into image prompts and story pages (sentences).
    For a clearer picture of how this works, reference prompts.py

    Attributes:
        _messages (list): A list of llm messages.
        _text (str): A buffer to store the text from text frames.
        _story (list): A list to store the story sentences, or 'pages'.

    Methods:
        process_frame: Processes a frame and removes any [break] or [image] tokens.
    """

    def __init__(self, messages, story):
        super().__init__()
        self._messages = messages
        self._text = ""
        self._story = story

    async def process_frame(self, frame: Frame, direction: FrameDirection):
        await super().process_frame(frame, direction)

        if isinstance(frame, UserStoppedSpeakingFrame):
            # Send an app message to the UI
            await self.push_frame(DailyTransportMessageFrame(CUE_ASSISTANT_TURN))
            await self.push_frame(sounds["talking"])

        elif isinstance(frame, TextFrame):
            # Add new text to the buffer
            self._text += frame.text
            # Process any complete patterns in the order they appear
            await self.process_text_content()

        # End of a full LLM response
        # Driven by the prompt, the LLM should have asked the user for input
        elif isinstance(frame, LLMFullResponseEndFrame):
            # We use a different frame type, as to avoid image generation ingest
            await self.push_frame(StoryPromptFrame(self._text))
            self._text = ""
            await self.push_frame(frame)
            # Send an app message to the UI
            await self.push_frame(DailyTransportMessageFrame(CUE_USER_TURN))
            await self.push_frame(sounds["listening"])

        # Anything that is not a TextFrame pass through
        else:
            await self.push_frame(frame)

    async def process_text_content(self):
        """Process text content in order of appearance, handling both image prompts and story breaks."""
        while True:
            # Find the first occurrence of each pattern
            image_match = re.search(r"<(.*?)>", self._text)
            break_match = re.search(r"\[[bB]reak\]", self._text)

            # If neither pattern is found, we're done processing
            if not image_match and not break_match:
                break

            # Find which pattern comes first in the text
            image_pos = image_match.start() if image_match else float("inf")
            break_pos = break_match.start() if break_match else float("inf")

            if image_pos < break_pos:
                # Process image prompt first
                image_prompt = image_match.group(1)
                # Remove the image prompt from the text
                self._text = self._text[: image_match.start()] + self._text[image_match.end() :]
                await self.push_frame(StoryImageFrame(image_prompt))
            else:
                # Process story break first
                parts = re.split(r"\[[bB]reak\]", self._text, flags=re.IGNORECASE, maxsplit=1)
                before_break = parts[0].replace("\n", " ").strip()

                if len(before_break) > 2:
                    self._story.append(before_break)
                    await self.push_frame(StoryPageFrame(before_break))
                    # await self.push_frame(sounds["ding"])
                    await self.push_frame(DailyTransportMessageFrame(CUE_ASSISTANT_TURN))

                # Keep the remainder (if any) in the buffer
                self._text = parts[1].strip() if len(parts) > 1 else ""
