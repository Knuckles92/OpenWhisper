import QtQuick
import QtQuick.Layouts
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Ui

BarWidget {
  id: root
  moduleName: "org.openwhisper.controls"
  property var status: ({})
  property double now: Date.now()
  readonly property bool connected: status.available === true && now - (status.updated || 0) < 7000
  readonly property bool busy: connected && (status.recording || status.transcribing || status.meeting)
  readonly property string label: !connected ? "OpenWhisper · click to open" :
    status.meeting ? "Meeting in progress" : status.recording ? "Recording · click to stop" :
    status.transcribing ? "Transcribing…" : status.enabled ? "OpenWhisper · click to record" : "OpenWhisper shortcuts paused"
  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  function trigger(action) {
    if (!connected) {
      Quickshell.execDetached(["openwhisper"])
      return
    }
    Quickshell.execDetached([
      "gdbus", "call", "--session", "--dest", "org.openwhisper.OpenWhisper",
      "--object-path", "/org/openwhisper/OpenWhisper", "--method", "org.openwhisper.Control.Button",
      action
    ])
  }

  Timer { interval: 1000; running: true; repeat: true; onTriggered: root.now = Date.now() }
  FileView {
    id: stateFile
    path: (Quickshell.env("XDG_RUNTIME_DIR") || "/run/user/" + Quickshell.env("UID")) + "/openwhisper/status.json"
    watchChanges: true
    onFileChanged: reload()
    onLoaded: {
      try { root.status = JSON.parse(text()); root.now = Date.now() }
      catch (error) { root.status = ({}) }
    }
  }

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.status.transcribing && root.connected ? "󰔟" : "󰍬"
    active: root.busy
    tooltipText: root.label + "\nRight-click: cancel · Middle-click: open"
    onPressed: function(mouseButton) {
      if (mouseButton === Qt.MiddleButton) root.trigger("show")
      else if (mouseButton === Qt.RightButton) root.trigger("cancel")
      else root.trigger(root.status.meeting ? "meeting" : "record")
    }
  }

  PopupCard {
    id: preview
    anchorItem: button
    bar: root.bar
    triggerMode: "hover"
    open: root.connected && !root.status.window_active && (root.status.recording || root.status.transcribing)
    contentWidth: fittedContentWidth(Style.space(340))
    contentHeight: fittedContentHeight(copy.implicitHeight, Style.space(240))
    ColumnLayout {
      id: copy
      anchors.fill: parent
      spacing: Style.space(8)
      Text {
        Layout.fillWidth: true
        text: root.label
        color: Color.popups.text
        font.family: Style.font.family
        font.pixelSize: Style.font.body
        font.bold: true
        wrapMode: Text.Wrap
      }
      Text {
        Layout.fillWidth: true
        Layout.fillHeight: true
        text: root.status.preview ? root.status.preview.slice(-360) :
          root.status.transcribing ? "Transcribing your recording…" : "Listening…"
        color: Color.popups.text
        font.family: Style.font.family
        font.pixelSize: Style.font.body
        wrapMode: Text.Wrap
        maximumLineCount: 7
        elide: Text.ElideLeft
        clip: true
      }
    }
  }
}
