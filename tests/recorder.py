"""A recording stand-in for any Resolve object, for tests of command modules.

    tl = Rec("timeline", GetName="Edit v1", GetTrackCount=lambda kind: 2)
    tl.SetName("x")                      -> True (default return value)
    tl.calls                             -> [("SetName", ("x",))]
    tl.called("SetName")                 -> [("x",)]

Return values: `returns[name]` if given (a callable is called with the arguments), else True.
Build a whole session with rec_resolve(), which wires resolve -> pm -> project -> timeline/mediapool.
"""


class Rec:
    def __init__(self, label="obj", **returns):
        self.__dict__["label"] = label
        self.__dict__["returns"] = dict(returns)
        self.__dict__["calls"] = []

    def __getattr__(self, name):
        if name.startswith("__"):
            raise AttributeError(name)
        if name.isupper() and name in self.returns:
            return self.returns[name]  # resolve.* constants are values, as on the real object

        def method(*args):
            self.calls.append((name, args))
            value = self.returns.get(name, True)
            return value(*args) if callable(value) else value

        return method

    def set(self, **returns):
        self.returns.update(returns)
        return self

    def called(self, name):
        return [args for method, args in self.calls if method == name]

    def __repr__(self):
        return f"Rec({self.label})"


def rec_resolve(project_name="Promo", timeline_name="Edit v1", items=None, clips=None):
    """A Resolve whose current project has one current timeline and a media pool root folder.

    items: list of Rec timeline items on video track 1; clips: list of Rec media pool items in the root.
    Returns (resolve, parts) where parts has pm, project, timeline, mediapool, root, storage, gallery, fusion.
    """
    items = items if items is not None else [Rec("item1", GetName="shot_010", GetUniqueId="item-1"),
                                             Rec("item2", GetName="shot_020", GetUniqueId="item-2")]
    clips = clips if clips is not None else [Rec("clip1", GetName="a.mov", GetUniqueId="clip-1")]
    root = Rec("root", GetName="Master", GetUniqueId="folder-root", GetClipList=clips, GetSubFolderList=[])
    mediapool = Rec("mediapool", GetRootFolder=root, GetCurrentFolder=root)
    timeline = Rec(
        "timeline", GetName=timeline_name, GetUniqueId="tl-1", GetTrackCount=lambda kind: 1 if kind == "video" else 0,
        GetItemListInTrack=lambda kind, index: items if (kind, index) == ("video", 1) else [],
        GetStartFrame=86400, GetEndFrame=86400 + 240, GetStartTimecode="01:00:00:00",
    )
    gallery = Rec("gallery")
    project = Rec(
        "project", GetName=project_name, GetUniqueId="project-1", GetCurrentTimeline=timeline,
        GetTimelineCount=1, GetTimelineByIndex=lambda i: timeline if i == 1 else None,
        GetMediaPool=mediapool, GetGallery=gallery,
    )
    pm = Rec("pm", GetCurrentProject=project)
    storage = Rec("storage")
    fusion = Rec("fusion")
    resolve = Rec("resolve", GetProjectManager=pm, GetMediaStorage=storage, Fusion=fusion,
                  GetProductName="DaVinci Resolve Studio", GetVersionString="21.1.0")
    parts = {"pm": pm, "project": project, "timeline": timeline, "mediapool": mediapool, "root": root,
             "storage": storage, "gallery": gallery, "fusion": fusion, "items": items, "clips": clips}
    return resolve, parts
