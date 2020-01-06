# ##### BEGIN GPL LICENSE BLOCK #####
#
#  This program is free software; you can redistribute it and/or
#  modify it under the terms of the GNU General Public License
#  as published by the Free Software Foundation; either version 2
#  of the License, or (at your option) any later version.
#
#  This program is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU General Public License for more details.
#
#  You should have received a copy of the GNU General Public License
#  along with this program; if not, write to the Free Software Foundation,
#  Inc., 51 Franklin Street, Fifth Floor, Boston, MA 02110-1301, USA.
#
# ##### END GPL LICENSE BLOCK #####

from math import pi

import bpy
import bmesh.ops
from bpy.props import IntProperty, FloatProperty, EnumProperty
from mathutils import Matrix, Vector

from sverchok.node_tree import SverchCustomTreeNode, throttled
from sverchok.data_structure import updateNode, match_long_repeat, fullList
from sverchok.utils.sv_bmesh_utils import bmesh_from_pydata, pydata_from_bmesh, fill_faces_layer

vsock, toposock = 'SvVerticesSocket', 'SvStringsSocket'

MASK = 0
OUT = 1
IN = 2
MASK_MEANING = {MASK: 'mask', OUT: 'out', IN: 'in'}

class SvExtrudeSeparateNode(bpy.types.Node, SverchCustomTreeNode):
    ''' Inset like behaviour '''
    bl_idname = 'SvExtrudeSeparateNode'
    bl_label = 'Extrude Separate Faces'
    bl_icon = 'OUTLINER_OB_EMPTY'
    sv_icon = 'SV_EXTRUDE_FACE'

    extrude_modes = [
            ("NORMAL", "Normal", "Extrude along normal and scale", 0),
            ("MATRIX", "Matrix", "Apply specified matrix", 1)
        ]

    @throttled
    def update_mode(self, context):
        self.inputs['Height'].hide_safe = self.extrude_mode != 'NORMAL'
        self.inputs['Scale'].hide_safe = self.extrude_mode != 'NORMAL'
        if 'Matrix' in self.inputs:
            self.inputs['Matrix'].hide_safe = self.extrude_mode != 'MATRIX'

    extrude_mode: EnumProperty(
            name = "Mode",
            description = "Extrusion mode",
            items = extrude_modes,
            default = 'NORMAL',
            update = update_mode)

    mask_modes = [
            ("NOEXTRUDE", "Do not extrude", "Do not perform extrusion on faces that are masked out", 0),
            ("NOTRANSFORM", "Do not transform", "Perform extrusion operator on all faces, but do not transform (move, scale) faces that are masked out", 1)
        ]

    mask_mode: EnumProperty(
            name = "Mask mode",
            description = "What to do with faces that are masked out",
            items = mask_modes,
            default = "NOEXTRUDE",
            update = updateNode)

    height_: FloatProperty(name="Height", description="Extrusion amount", default=0.0, update=updateNode)
    scale_: FloatProperty(name="Scale", description="Extruded faces scale", default=1.0, min=0.0, update=updateNode)

    mask_type_items = [
            ('mask', "Mask", "Faces that were masked out"),
            ('out',  "Out", "Outer faces of the extrusion"),
            ('in',   "In",  "Inner faces of the extrusion"),
        ]

    mask_out_type : EnumProperty(
            name = "Mask Output",
            items=mask_type_items,
            update=updateNode,
            options={'ENUM_FLAG'},
            default={'out'},
            description="Switch between untouched, inner and outer faces generated by insertion")

    replacement_nodes = [
        ('SvExtrudeSeparateLiteNode', None, None),
        ('SvInsetSpecial',
            dict(Vertices='vertices', Polygons='polygons'),
            dict(Vertices='vertices', Polygons='polygons')),
        ('SvInsetFaces',
            dict(Vertices='Verts', Polygons='Faces'),
            dict(Vertices='Verts', Polygons='Faces'))
    ]

    def sv_init(self, context):
        inew = self.inputs.new
        onew = self.outputs.new

        inew(vsock, "Vertices")
        inew(toposock, 'Edges')
        inew(toposock, 'Polygons')
        inew(toposock, 'Mask')
        inew(toposock, "Height").prop_name = "height_"
        inew(toposock, "Scale").prop_name = "scale_"
        inew('SvMatrixSocket', 'Matrix')

        onew(vsock, 'Vertices')
        onew(toposock, 'Edges')
        onew(toposock, 'Polygons')
        onew(toposock, 'ExtrudedPolys')
        onew(toposock, 'OtherPolys')
        onew('SvStringsSocket', 'Mask').custom_draw = 'draw_mask_socket'

        self.update_mode(context)

    def draw_mask_socket(self, socket, context, layout):
        layout.prop(self, 'mask_out_type', expand=True)
        layout.label(text=socket.name)

    def draw_buttons(self, context, layout):
        layout.prop(self, 'extrude_mode')

    def draw_buttons_ext(self, context, layout):
        self.draw_buttons(context, layout)
        layout.prop(self, 'mask_mode')

    @property
    def scale_socket_type(self):
        socket = self.inputs['Scale']
        if socket.is_linked:
            other = socket.other
            if other.bl_idname == 'SvVerticesSocket':
                print('connected a Vector Socket')
                return True
        return False

    def get_out_mask(self, bm, extruded_faces):
        mask_layer = bm.faces.layers.int.get('mask')
        for face in extruded_faces:
            face[mask_layer] = IN
        mask = [int(MASK_MEANING[face[mask_layer]] in self.mask_out_type) for face in bm.faces]
        return mask

    def process(self):

        inputs = self.inputs
        outputs = self.outputs

        if not (inputs['Vertices'].is_linked and inputs['Polygons'].is_linked):
            return
        if not any(socket.is_linked for socket in outputs):
            return

        need_mask_out = 'Mask' in outputs and outputs['Mask'].is_linked

        vector_in = self.scale_socket_type

        vertices_s = inputs['Vertices'].sv_get()
        edges_s = inputs['Edges'].sv_get(default=[[]])
        faces_s = inputs['Polygons'].sv_get(default=[[]])
        masks_s = inputs['Mask'].sv_get(default=[[1]])
        heights_s = inputs['Height'].sv_get()
        scales_s  = inputs['Scale'].sv_get()
        if 'Matrix' in inputs:
            matrixes_s = inputs['Matrix'].sv_get(default=[[Matrix()]])
        else:
            matrixes_s = [[Matrix()]]

        if type(matrixes_s[0]) == Matrix:
            matrixes_s = [matrixes_s]

        linked_extruded_polygons = outputs['ExtrudedPolys'].is_linked
        linked_other_polygons = outputs['OtherPolys'].is_linked

        result_vertices = []
        result_edges = []
        result_faces = []
        result_extruded_faces = []
        result_other_faces = []
        result_mask = []

        meshes = match_long_repeat([vertices_s, edges_s, faces_s, masks_s, heights_s, scales_s, matrixes_s])

        for vertices, edges, faces, masks, heights, scales, matrixes in zip(*meshes):

            new_extruded_faces = []
            new_extruded_faces_append = new_extruded_faces.append
            fullList(heights, len(faces))
            fullList(scales, len(faces))
            fullList(matrixes, len(faces))
            fullList(masks, len(faces))

            bm = bmesh_from_pydata(vertices, edges, faces)
            mask_layer = bm.faces.layers.int.new('mask')
            bm.faces.ensure_lookup_table()
            fill_faces_layer(bm, masks, 'mask', int, OUT)

            if self.mask_mode == 'NOEXTRUDE':
                faces_to_extrude = [face for face, mask in zip(bm.faces, masks) if mask]
            else:
                faces_to_extrude = bm.faces

            extruded_faces = bmesh.ops.extrude_discrete_faces(bm, faces=faces_to_extrude)['faces']

            if self.mask_mode == 'NOEXTRUDE':
                face_data = zip(extruded_faces, heights, scales, matrixes)
            else:
                face_data = [(face, height, scale, matrix) for (face, mask, height, scale, matrix) in zip(extruded_faces, masks, heights, scales, matrixes) if mask]

            for face, height, scale, matrix in face_data:

                vec = scale if vector_in else (scale, scale, scale)

                # preparing matrix
                normal = face.normal
                if normal[0] == 0 and normal[1] == 0:
                    m_r = Matrix() if normal[2] >= 0 else Matrix.Rotation(pi, 4, 'X')
                else:
                    z_axis = normal
                    x_axis = (Vector((z_axis[1] * -1, z_axis[0], 0))).normalized()
                    y_axis = (z_axis.cross(x_axis)).normalized()
                    m_r = Matrix(list([*zip(x_axis[:], y_axis[:], z_axis[:])])).to_4x4()

                dr = face.normal * height
                center = face.calc_center_median()
                translation = Matrix.Translation(center)
                space = (translation @ m_r).inverted()

                if self.extrude_mode == 'NORMAL':
                    # inset, scale and push operations
                    bmesh.ops.scale(bm, vec=vec, space=space, verts=face.verts)
                    bmesh.ops.translate(bm, verts=face.verts, vec=dr)
                else:
                    bmesh.ops.transform(bm, matrix=matrix, space=space, verts=face.verts)

                if linked_extruded_polygons or linked_other_polygons:
                    new_extruded_faces_append([v.index for v in face.verts])

            new_vertices, new_edges, new_faces = pydata_from_bmesh(bm)

            new_other_faces = [f for f in new_faces if f not in new_extruded_faces] if linked_other_polygons else []
            if need_mask_out:
                new_mask = self.get_out_mask(bm, extruded_faces)
                result_mask.append(new_mask)

            bm.free()

            result_vertices.append(new_vertices)
            result_edges.append(new_edges)
            result_faces.append(new_faces)
            result_extruded_faces.append(new_extruded_faces)
            result_other_faces.append(new_other_faces)

        outputs['Vertices'].sv_set(result_vertices)
        outputs['Edges'].sv_set(result_edges)
        outputs['Polygons'].sv_set(result_faces)
        outputs['ExtrudedPolys'].sv_set(result_extruded_faces)
        outputs['OtherPolys'].sv_set(result_other_faces)
        if need_mask_out:
            outputs['Mask'].sv_set(result_mask)


def register():
    bpy.utils.register_class(SvExtrudeSeparateNode)


def unregister():
    bpy.utils.unregister_class(SvExtrudeSeparateNode)
