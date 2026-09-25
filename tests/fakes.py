"""In-memory stand-ins for the Resolve scripting objects dava calls."""


class FakeClip:
    READ_ONLY_PROPS = {"FPS"}

    def __init__(self, name):
        self.name = name
        self.uid = "clip-" + name
        self.metadata = {"Scene": "1"}
        self.third_party = {}
        self.props = {"FPS": "24", "Clip Name": name}
        self.color = ""

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return self.uid

    def GetMetadata(self):
        return dict(self.metadata)

    def SetMetadata(self, values):
        self.metadata.update(values)
        return True

    def GetThirdPartyMetadata(self):
        return dict(self.third_party)

    def SetThirdPartyMetadata(self, values):
        self.third_party.update(values)
        return True

    def GetClipProperty(self):
        return dict(self.props)

    def SetClipProperty(self, key, value):
        if key in self.READ_ONLY_PROPS:
            return False
        self.props[key] = value
        return True

    def GetClipColor(self):
        return self.color

    def SetClipColor(self, color):
        self.color = color
        return True

    def ClearClipColor(self):
        self.color = ""
        return True


class FakeGraph:
    def __init__(self, nodes=2):
        self.luts = {n: "" for n in range(1, nodes + 1)}
        self.drx = []
        self.reset = False

    def GetNumNodes(self):
        return len(self.luts)

    def GetNodeLabel(self, node):
        return f"Node {node}"

    def GetLUT(self, node):
        return self.luts[node]

    def SetLUT(self, node, path):
        if not path.endswith(".cube"):
            return False
        self.luts[node] = path
        return True

    def GetToolsInNode(self, node):
        return ["Primary"] if node == 1 else []

    def ApplyGradeFromDRX(self, path, mode):
        self.drx.append((path, mode))
        return True

    def ResetAllGrades(self):
        self.reset = True
        return True


class FakeItem:
    def __init__(self, name):
        self.name = name
        self.uid = "item-" + name
        self.graph = FakeGraph()
        self.clip = FakeClip(name + ".mov")
        self.cdls = []
        self.comps = []
        self.lut_exports = []

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return self.uid

    def GetMediaPoolItem(self):
        return self.clip

    def GetNodeGraph(self, layer=None):
        if layer not in (None, 1):
            return None
        return self.graph

    def GetFusionCompByIndex(self, index):
        return FakeComp(self.comps[index - 1]) if 1 <= index <= len(self.comps) else None

    def GetFusionCompByName(self, name):
        return FakeComp(name) if name in self.comps else None

    def SetCDL(self, cdl):
        self.cdls.append(cdl)
        return True

    def ExportLUT(self, export_type, path):
        self.lut_exports.append((export_type, path))
        return True

    def GetFusionCompNameList(self):
        return list(self.comps)

    def GetFusionCompCount(self):
        return len(self.comps)

    def AddFusionComp(self):
        self.comps.append(f"Composition {len(self.comps) + 1}")
        return object()

    def ImportFusionComp(self, path):
        self.comps.append(path.rsplit("/", 1)[-1])
        return object()

    def ExportFusionComp(self, path, index):
        with open(path, "w") as handle:
            handle.write(self.comps[index - 1])
        return True

    def DeleteFusionCompByName(self, name):
        if name not in self.comps:
            return False
        self.comps.remove(name)
        return True

    def RenameFusionCompByName(self, old, new):
        if old not in self.comps:
            return False
        self.comps[self.comps.index(old)] = new
        return True


class FakeComp:
    def __init__(self, name):
        self.name = name

    def GetAttrs(self, key):
        return {"COMPS_Name": self.name}[key]


class FakeStill:
    def __init__(self, label):
        self.label = label


class FakeAlbum:
    def __init__(self, name, labels=()):
        self.name = name
        self.stills = [FakeStill(label) for label in labels]
        self.exports = []

    def GetStills(self):
        return list(self.stills)

    def GetLabel(self, still):
        return still.label

    def ExportStills(self, stills, folder, prefix, fmt):
        self.exports.append((len(stills), folder, prefix, fmt))
        return True


class FakeGallery:
    def __init__(self):
        self.albums = [FakeAlbum("Stills 1", ["1.1.1", "1.2.1"]), FakeAlbum("Looks", ["warm"])]
        self.current = self.albums[0]

    def GetAlbumName(self, album):
        return album.name

    def GetCurrentStillAlbum(self):
        return self.current

    def GetGalleryStillAlbums(self):
        return list(self.albums)


class FakeFolder:
    def __init__(self, name, clips=(), subfolders=()):
        self.name = name
        self.clips = [FakeClip(c) for c in clips]
        self.subfolders = list(subfolders)

    def GetName(self):
        return self.name

    def GetClipList(self):
        return self.clips

    def GetSubFolderList(self):
        return self.subfolders

    def GetUniqueId(self):
        return "folder-" + self.name


class FakeMediaPool:
    def __init__(self, project):
        self.project = project
        self.root = FakeFolder("Master", ["a.mov"], [FakeFolder("Audio", ["vo.wav"])])
        self.imported = []
        self.appended = []

    def GetRootFolder(self):
        return self.root

    def GetCurrentFolder(self):
        return self.root

    def CreateEmptyTimeline(self, name):
        if any(t.name == name for t in self.project.timelines):
            return None
        timeline = FakeTimeline(name)
        self.project.timelines.append(timeline)
        return timeline

    def ImportMedia(self, infos):
        self.imported.extend(infos)
        return [FakeClip(info["FilePath"].rsplit("/", 1)[-1]) for info in infos]

    def AppendToTimeline(self, infos):
        self.appended.extend(infos)
        return [FakeItem(info["mediaPoolItem"].GetName()) for info in infos]


class FakeTimeline:
    def __init__(self, name):
        self.name = name
        self.markers = {}
        self.exports = []
        self.tracks = {1: [FakeItem("shot_010"), FakeItem("shot_020")]}
        self.gallery = None
        self.graph = FakeGraph(nodes=1)

    def GetItemListInTrack(self, track_type, index):
        return self.tracks.get(index, []) if track_type == "video" else []

    def GetCurrentVideoItem(self):
        return self.tracks[1][0]

    def GetNodeGraph(self):
        return self.graph

    def GrabStill(self):
        self.gallery.current.stills.append(FakeStill("grabbed"))
        return self.gallery.current.stills[-1]

    def GrabAllStills(self, source):
        assert source in (1, 2)
        grabbed = [FakeStill(f"grab-{source}") for _ in self.tracks[1]]
        self.gallery.current.stills.extend(grabbed)
        return grabbed

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return "id-" + self.name

    def SetName(self, name):
        if not name:
            return False
        self.name = name
        return True

    def DeleteClips(self, items, ripple=False):
        self.deleted = (list(items), ripple)
        return True

    def GetStartTimecode(self):
        return "01:00:00:00"

    def GetTrackCount(self, track_type):
        return {"video": 2, "audio": 4}.get(track_type, 0)

    def GetMarkers(self):
        return self.markers

    def AddMarker(self, frame, color, name, note, duration):
        if frame in self.markers:
            return False
        self.markers[frame] = {"color": color, "duration": duration, "note": note, "name": name, "customData": ""}
        return True

    def Export(self, path, export_type, export_subtype):
        self.exports.append((path, export_type, export_subtype))
        return True


class FakeColorGroup:
    def __init__(self, name):
        self.name = name
        self.pre = FakeGraph(nodes=1)
        self.post = FakeGraph(nodes=3)

    def GetName(self):
        return self.name

    def GetPreClipNodeGraph(self):
        return self.pre

    def GetPostClipNodeGraph(self):
        return self.post


class FakeProject:
    def __init__(self, name):
        self.name = name
        self.timelines = [FakeTimeline("Edit v1"), FakeTimeline("Edit v2")]
        self.current = self.timelines[0]
        self.media_pool = FakeMediaPool(self)
        self.gallery = FakeGallery()
        for timeline in self.timelines:
            timeline.gallery = self.gallery
        self.lut_refreshes = 0
        self.color_groups = [FakeColorGroup("Warm")]
        self.jobs = []
        self.job_status = {}
        self.render_settings = {}
        self.preset = None
        self.started = None
        self.render_polls = 0

    def GetName(self):
        return self.name

    def GetUniqueId(self):
        return "project-" + self.name

    def GetColorGroupsList(self):
        return self.color_groups

    def GetMediaPool(self):
        return self.media_pool

    def GetTimelineCount(self):
        return len(self.timelines)

    def GetTimelineByIndex(self, idx):
        assert idx >= 1, "Resolve timeline indices are 1-based"
        return self.timelines[idx - 1]

    def GetCurrentTimeline(self):
        return self.current

    def SetCurrentTimeline(self, timeline):
        self.current = timeline
        return True

    def GetGallery(self):
        return self.gallery

    def RefreshLUTList(self):
        self.lut_refreshes += 1
        return True

    def GetSettings(self):
        return {"timelineFrameRate": "24", "timelineResolutionWidth": "1920"}

    def GetRenderPresetList(self):
        return ["H.264 Master", "YouTube 1080p"]

    def LoadRenderPreset(self, name):
        if name not in self.GetRenderPresetList():
            return False
        self.preset = name
        return True

    def SetRenderSettings(self, settings):
        self.render_settings.update(settings)
        return True

    def AddRenderJob(self):
        job_id = f"job-{len(self.jobs) + 1:04d}-uuid"
        self.jobs.append({
            "JobId": job_id,
            "TimelineName": self.current.name,
            "TargetDir": self.render_settings.get("TargetDir", "/out"),
            "OutputFilename": self.render_settings.get("CustomName", "out") + ".mov",
        })
        self.job_status[job_id] = {"JobStatus": "Ready", "CompletionPercentage": 0}
        return job_id

    def GetRenderJobList(self):
        return self.jobs

    def GetRenderJobStatus(self, job_id):
        return self.job_status[job_id]

    def StartRendering(self, job_ids):
        self.started = list(job_ids)
        return True

    def IsRenderingInProgress(self):
        # Report in-progress once, then mark started jobs complete.
        self.render_polls += 1
        if self.render_polls > 1:
            for job_id in self.started:
                self.job_status[job_id] = {"JobStatus": "Complete", "CompletionPercentage": 100}
            return False
        return True


class FakeProjectManager:
    def __init__(self):
        self.projects = {"Promo": FakeProject("Promo"), "Doc": FakeProject("Doc")}
        self.current = self.projects["Promo"]

    def GetCurrentProject(self):
        return self.current

    def GetProjectListInCurrentFolder(self):
        return list(self.projects)

    def LoadProject(self, name):
        self.current = self.projects.get(name)
        return self.current

    def CreateProject(self, name):
        if name in self.projects:
            return None
        self.projects[name] = self.current = FakeProject(name)
        return self.current

    def SaveProject(self):
        return True


class FakeResolve:
    EXPORT_EDL = 3.0
    EXPORT_NONE = 0.0
    EXPORT_AAF = 1.0
    EXPORT_AAF_NEW = 1.0
    EXPORT_OTIO = 15.0
    EXPORT_LUT_33PTCUBE = 1.0
    EXPORT_LUT_65PTCUBE = 2.0
    EXPORT_TEXT_CSV = 9.0

    def __init__(self):
        self.pm = FakeProjectManager()
        self.page = "edit"

    def GetProjectManager(self):
        return self.pm

    def GetProductName(self):
        return "DaVinci Resolve Studio"

    def GetVersionString(self):
        return "21.1.0"

    def GetCurrentPage(self):
        return self.page

    def GetMediaStorage(self):
        return FakeMediaStorage()

    def Fusion(self):
        return object()

    def Quit(self):
        self.quit = True
        return True

    def OpenPage(self, name):
        self.page = name
        return True


class FakeMediaStorage:
    def __init__(self):
        self.clones = []

    def StartCloneMedia(self, source, targets):
        self.clones.append((source, targets))
        return True
