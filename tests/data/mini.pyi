''' Test fixture: a small slice of DaVinciResolveScript.pyi with the same shapes as the real stub. '''

from typing import TypedDict, Literal, TypeAlias

MarkerColor = Literal['Blue', 'Cyan', 'Green', 'Red']
TrackType = Literal['video', 'audio', 'subtitle']

TimelineExportType: TypeAlias = float
"""One of the resolve.* constants: EXPORT_EDL, EXPORT_TEXT_CSV"""
TimelineExportSubtype: TypeAlias = float
"""One of the resolve.* constants: EXPORT_NONE, EXPORT_CDL"""

class AppendClipInfo(TypedDict, total=False):
	mediaPoolItem: 'MediaPoolItem'
	"""MediaPoolItem object to append"""
	startFrame: float
	"""Source start frame (optional)"""

class MarkerInfo(TypedDict, total=False):
	color: MarkerColor
	"""Color name"""
	name: str
	"""Name"""

ClipProperties = TypedDict('ClipProperties', {'Clip Name': str, 'FPS': float}, total=False)

class Resolve:
	"""The Resolve application."""
	EXPORT_EDL: TimelineExportType
	EXPORT_TEXT_CSV: TimelineExportType
	EXPORT_NONE: TimelineExportSubtype
	EXPORT_CDL: TimelineExportSubtype
	def GetProductName(self) -> str:
		"""Returns product name."""
		...
	def GetMediaStorage(self) -> MediaStorage:
		"""Returns the media storage."""
		...
	def Quit(self) -> bool:
		"""Quits the Resolve App"""
		...

class Project:
	"""A project."""
	def GetName(self) -> str:
		"""Returns project name."""
		...
	def GetCurrentTimeline(self) -> Timeline:
		"""Returns the currently loaded timeline."""
		...
	def SetCurrentTimeline(self, timeline: Timeline) -> bool:
		"""Sets given timeline as current timeline for the project"""
		...
	def GetTimelineByIndex(self, idx: int) -> Timeline | None:
		"""Returns timeline at the given index, 1 <= idx <= project.GetTimelineCount()"""
		...

class MediaPool:
	"""The media pool."""
	def GetRootFolder(self) -> Folder:
		"""Returns root Folder of Media Pool"""
		...
	def AppendToTimeline(self, clipInfos: list[AppendClipInfo]) -> list[TimelineItem]:
		"""Appends specified MediaPoolItem objects in the current timeline."""
		...

class MediaPoolItem:
	"""A clip in the media pool."""
	def GetName(self) -> str:
		"""Returns the clip name."""
		...
	def GetMetadata(self, metadataType: str | None = None) -> str | dict:
		"""Returns the metadata value for the key 'metadataType'."""
		...
	def SetMetadata(self, metadata: dict) -> bool:
		"""Sets the item metadata with specified dict of key-value pairs"""
		...
	def GetClipProperty(self, propertyName: str | None = None) -> str | ClipProperties:
		"""Returns the property value."""
		...

class Folder:
	"""A media pool folder."""
	def GetName(self) -> str:
		"""Returns the folder name."""
		...
	def GetClipList(self) -> list[MediaPoolItem]:
		"""Returns a list of clips (items) within the folder."""
		...

class Timeline:
	"""A timeline."""
	def GetName(self) -> str:
		"""Returns the timeline name."""
		...
	def SetName(self, timelineName: str) -> bool:
		"""Sets the timeline name if timelineName (string) is unique."""
		...
	def GetItemListInTrack(self, trackType: TrackType, index: int) -> list[TimelineItem]:
		"""Returns a list of timeline items on specified track."""
		...
	def AddMarker(self, frameId: int, color: MarkerColor, name: str, note: str, duration: int, customData: str | None = None) -> bool:
		"""Creates a new marker at given frameId position."""
		...
	def GetMarkers(self) -> dict[int, MarkerInfo]:
		"""Returns a dict (frameId -> {information}) of all markers."""
		...
	def Export(self, fileName: str, exportType: TimelineExportType, exportSubtype: TimelineExportSubtype) -> bool:
		"""Exports timeline."""
		...
	def DeleteClips(self, timelineItems: list[TimelineItem], rippleDelete = False) -> bool:
		"""Deletes specified TimelineItems from the timeline."""
		...
	def GetNodeGraph(self) -> Graph:
		"""Returns the timeline's node graph object."""
		...

class TimelineItem:
	"""A clip on a timeline track."""
	def GetName(self) -> str:
		"""Returns the item name"""
		...
	def GetStart(self, subframePrecision = False) -> float:
		"""Returns the start frame position on the timeline."""
		...
	def GetNodeGraph(self, layerIdx: int | None = None) -> Graph:
		"""Returns the clip's node graph object at layerIdx."""
		...
	def GetMediaPoolItem(self) -> MediaPoolItem | None:
		"""Returns the media pool item corresponding to the timeline item if one exists"""
		...

class ColorGroup:
	"""A group of clips."""
	def GetName(self) -> str:
		"""Returns the name."""
		...
	def GetPostClipNodeGraph(self) -> Graph:
		"""Returns the post-clip graph."""
		...

class Graph:
	"""The node graph of a clip."""
	def GetNumNodes(self) -> int:
		"""Returns the number of nodes in the graph"""
		...
	def SetLUT(self, nodeIndex: int, lutPath: str) -> bool:
		"""Sets LUT on the node mapping the node index provided"""
		...

class MediaStorage:
	"""Browses the volumes of the file system."""
	def StartCloneMedia(self, sourceDir: str, targetDirs: str | list[str]) -> bool:
		"""Starts cloning media from sourceDir to targetDirs."""
		...

class GalleryStill:
	"""A still of a gallery album."""

class Fusion: ...
class FusionComp: ...
def scriptapp(app: str) -> Resolve: ...
