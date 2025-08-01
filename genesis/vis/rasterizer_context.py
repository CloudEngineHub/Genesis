import numpy as np
import trimesh

import taichi as ti

import genesis as gs
import genesis.utils.geom as gu
import genesis.utils.mesh as mu
import genesis.utils.particle as pu

from genesis.ext import pyrender
from genesis.ext.pyrender.jit_render import JITRenderer
from genesis.utils.misc import tensor_to_array


class RasterizerContext:
    def __init__(self, options):
        self.show_world_frame = options.show_world_frame
        self.world_frame_size = options.world_frame_size
        self.show_link_frame = options.show_link_frame
        self.link_frame_size = options.link_frame_size
        self.show_cameras = options.show_cameras
        self.shadow = options.shadow
        self.plane_reflection = options.plane_reflection
        self.ambient_light = options.ambient_light
        self.background_color = options.background_color
        self.segmentation_level = options.segmentation_level
        self.lights = options.lights
        self.visualize_mpm_boundary = options.visualize_mpm_boundary
        self.visualize_sph_boundary = options.visualize_sph_boundary
        self.visualize_pbd_boundary = options.visualize_pbd_boundary
        self.particle_size_scale = options.particle_size_scale
        self.contact_force_scale = options.contact_force_scale
        self.render_particle_as = options.render_particle_as
        self.rendered_envs_idx = options.rendered_envs_idx
        self.env_separate_rigid = options.env_separate_rigid

        self.buffer = dict()

        # nodes
        self.world_frame_node = None
        self.link_frame_nodes = dict()
        self.frustum_nodes = dict()  # nodes camera frustums
        self.rigid_nodes = dict()
        self.static_nodes = dict()  # used across all frames
        self.dynamic_nodes = list()  # nodes that live within single frame
        self.external_nodes = dict()  # nodes added by external user
        self.seg_node_map = dict()
        self.seg_idxc_map = {0: -1}
        self.seg_key_map = {-1: 0}

        self.init_meshes()

    def init_meshes(self):
        self.world_frame_shown = False
        self.link_frame_shown = False
        self.camera_frustum_shown = False

        self.world_frame_mesh = mu.create_frame(
            origin_radius=0.012,
            axis_radius=0.005,
            axis_length=self.world_frame_size,
            head_radius=0.01,
            head_length=0.03,
        )

        self.link_frame_mesh = trimesh.creation.axis(origin_size=0.03, axis_radius=0.025, axis_length=1.0)
        self.link_frame_mesh.visual.face_colors[:, :3] = (0.7 * self.link_frame_mesh.visual.face_colors[:, :3]).astype(
            int
        )
        self.link_frame_mesh.vertices *= self.link_frame_size

    def build(self, scene):
        self.scene = scene
        self.sim = scene.sim
        self.visualizer = scene.visualizer
        self.visualizer.update_visual_states()

        if self.rendered_envs_idx is None:
            self.rendered_envs_idx = list(range(self.sim._B))

        # pyrender scene
        self._scene = pyrender.Scene(
            ambient_light=self.ambient_light,
            bg_color=self.background_color,
            n_envs=len(self.rendered_envs_idx),
        )

        self.jit = JITRenderer(self._scene, [], [])

        self.on_lights()

        if self.show_world_frame:
            self.on_world_frame()
        if self.show_link_frame:
            self.on_link_frame()
        if self.show_cameras:
            self.on_camera_frustum()

        self.on_tool()
        self.on_rigid()
        self.on_avatar()
        self.on_mpm()
        self.on_sph()
        self.on_pbd()
        self.on_fem()

        # segmentation mapping
        self.generate_seg_vars()

    def destroy(self):
        self.clear_dynamic_nodes()

        for node_registry in (
            self.link_frame_nodes,
            self.frustum_nodes,
            self.rigid_nodes,
            self.static_nodes,
            self.external_nodes,
        ):
            for external_node in node_registry.values():
                self.remove_node(external_node)
            node_registry.clear()

    def reset(self):
        self._t = -1

    def add_node(self, obj, **kwargs):
        return self._scene.add(obj, **kwargs)

    def remove_node(self, node):
        self._scene.remove_node(node)

    def add_rigid_node(self, geom, obj, **kwargs):
        rigid_node = self.add_node(obj, **kwargs)
        self.rigid_nodes[geom.uid] = rigid_node

        # create segemtation id
        if self.segmentation_level == "geom":
            seg_key = (geom.entity.idx, geom.link.idx, geom.idx)
            assert False, "geom level segmentation not supported yet"
        elif self.segmentation_level == "link":
            seg_key = (geom.entity.idx, geom.link.idx)
        elif self.segmentation_level == "entity":
            seg_key = geom.entity.idx
        else:
            gs.raise_exception(f"Unsupported segmentation level: {self.segmentation_level}")
        self.create_node_seg(seg_key, rigid_node)

    def add_static_node(self, entity, obj, **kwargs):
        static_node = self.add_node(obj, **kwargs)
        self.static_nodes[entity.uid] = static_node
        self.create_node_seg(entity.idx, static_node)

    def add_dynamic_node(self, entity, obj, **kwargs):
        if obj:
            dynamic_node = self.add_node(obj, **kwargs)
            self.dynamic_nodes.append(dynamic_node)
        else:
            dynamic_node = None
        if entity:
            self.create_node_seg(entity.idx, dynamic_node)

    def add_external_node(self, obj, **kwargs):
        # Check if the node has a valid name
        if not hasattr(obj, "name") or not obj.name:
            gs.raise_exception("Node must have a valid 'name' attribute.")

        # Check if the name is already in use
        if obj.name in self.external_nodes:
            gs.raise_exception(f"A node with the name '{obj.name}' already exists.")

        self.external_nodes[obj.name] = self.add_node(obj, **kwargs)

    def clear_dynamic_nodes(self):
        for dynamic_node in self.dynamic_nodes:
            self.remove_node_seg(dynamic_node)
            self.remove_node(dynamic_node)
        self.dynamic_nodes.clear()

    def clear_external_node(self, node):
        if node.name in self.external_nodes:
            self.remove_node(self.external_nodes[node.name])
            del self.external_nodes[node.name]

    def clear_external_nodes(self):
        for external_node in self.external_nodes.values():
            self.remove_node(external_node)
        self.external_nodes.clear()

    def set_node_pose(self, node, pose):
        self._scene.set_pose(node, pose)

    def update_camera_frustum(self, camera):
        if self.camera_frustum_shown:
            self.set_node_pose(self.frustum_nodes[camera.uid], camera.transform)

    def on_camera_frustum(self):
        if not self.camera_frustum_shown:
            for camera in self.cameras:
                self.frustum_nodes[camera.uid] = self.add_node(
                    pyrender.Mesh.from_trimesh(
                        mu.create_camera_frustum(camera, color=(1.0, 1.0, 1.0, 0.3)),
                        smooth=False,
                    ),
                    pose=camera.transform,
                )
            self.camera_frustum_shown = True

    def off_camera_frustum(self):
        if self.camera_frustum_shown:
            for camera in self.cameras:
                self.remove_node(self.frustum_nodes[camera.uid])
            self.frustum_nodes.clear()
            self.camera_frustum_shown = False

    def on_world_frame(self):
        if not self.world_frame_shown:
            self.world_frame_node = self.add_node(pyrender.Mesh.from_trimesh(self.world_frame_mesh, smooth=True))
            self.world_frame_shown = True

    def off_world_frame(self):
        if self.world_frame_shown:
            self.remove_node(self.world_frame_node)
            self.world_frame_node = None
            self.world_frame_shown = False

    def on_link_frame(self):
        if not self.link_frame_shown:
            if self.sim.rigid_solver.is_active():
                links = self.sim.rigid_solver.links
                links_pos = self.sim.rigid_solver.links_state.pos.to_numpy() + self.scene.envs_offset[None, :]
                links_quat = self.sim.rigid_solver.links_state.quat.to_numpy()

                for link in links:
                    self.link_frame_nodes[link.uid] = self.add_node(
                        pyrender.Mesh.from_trimesh(
                            mesh=self.link_frame_mesh,
                            poses=gu.trans_quat_to_T(links_pos[link.idx], links_quat[link.idx]),
                            env_shared=not self.env_separate_rigid,
                        )
                    )
            self.link_frame_shown = True

    def off_link_frame(self):
        if self.link_frame_shown:
            for node in self.link_frame_nodes.values():
                self.remove_node(node)
            self.link_frame_nodes.clear()
            self.link_frame_shown = False

    def update_link_frame(self, buffer_updates):
        if self.link_frame_shown:
            if self.sim.rigid_solver.is_active():
                links = self.sim.rigid_solver.links

                links_pos = self.sim.rigid_solver.links_state.pos.to_numpy() + self.scene.envs_offset[None, :]
                links_quat = self.sim.rigid_solver.links_state.quat.to_numpy()

                for link in links:
                    link_T = gu.trans_quat_to_T(links_pos[link.idx], links_quat[link.idx])
                    node = self._scene.get_buffer_id(self.link_frame_nodes[link.uid], "model")
                    buffer_updates[node] = link_T.transpose((0, 2, 1))

    def on_tool(self):
        if self.sim.tool_solver.is_active():
            for tool_entity in self.sim.tool_solver.entities:
                if tool_entity.mesh is not None:
                    mesh = trimesh.Trimesh(
                        tool_entity.mesh.raw_vertices,
                        tool_entity.mesh.faces_np.reshape([-1, 3]),
                        tool_entity.mesh.raw_vertex_normals,
                        process=False,
                    )
                    mesh.visual = mu.surface_uvs_to_trimesh_visual(tool_entity.surface, n_verts=len(mesh.vertices))

                    pose = gu.trans_quat_to_T(tool_entity.init_pos, tool_entity.init_quat)
                    double_sided = tool_entity.surface.double_sided
                    self.add_static_node(
                        tool_entity, pyrender.Mesh.from_trimesh(mesh, double_sided=double_sided), pose=pose
                    )

    def update_tool(self, buffer_updates):
        if self.sim.tool_solver.is_active():
            for tool_entity in self.sim.tool_solver.entities:
                pos = tool_entity.pos[self.sim.cur_substep_local, 0].to_numpy()
                quat = tool_entity.quat[self.sim.cur_substep_local, 0].to_numpy()
                pose = gu.trans_quat_to_T(pos, quat)
                self.set_node_pose(self.static_nodes[tool_entity.uid], pose=pose)

    def set_reflection_mat(self, geom_T):
        height = geom_T[0, 2, 3]
        self.jit.reflection_mat = np.array(
            [
                [1.0, 0.0, 0.0, 0.0],
                [0.0, 1.0, 0.0, 0.0],
                [0.0, 0.0, -1.0, height * 2],
                [0.0, 0.0, 0.0, 1.0],
            ],
            dtype=np.float32,
        )

    def on_rigid(self):
        if self.sim.rigid_solver.is_active():
            # TODO: support dynamic switching in GUI later
            for rigid_entity in self.sim.rigid_solver.entities:
                if rigid_entity.surface.vis_mode == "visual":
                    geoms = rigid_entity.vgeoms
                    geoms_T = self.sim.rigid_solver._vgeoms_render_T
                else:
                    geoms = rigid_entity.geoms
                    geoms_T = self.sim.rigid_solver._geoms_render_T

                for geom in geoms:
                    if "sdf" in rigid_entity.surface.vis_mode:
                        mesh = geom.get_sdf_trimesh()
                    else:
                        mesh = geom.get_trimesh()
                    geom_T = geoms_T[geom.idx][self.rendered_envs_idx]
                    self.add_rigid_node(
                        geom,
                        pyrender.Mesh.from_trimesh(
                            mesh=mesh,
                            poses=geom_T,
                            smooth=geom.surface.smooth if "collision" not in rigid_entity.surface.vis_mode else False,
                            double_sided=(
                                geom.surface.double_sided if "collision" not in rigid_entity.surface.vis_mode else False
                            ),
                            is_floor=isinstance(rigid_entity._morph, gs.morphs.Plane),
                            env_shared=not self.env_separate_rigid,
                        ),
                    )
                    if isinstance(rigid_entity._morph, gs.morphs.Plane):
                        self.set_reflection_mat(geom_T)

    def update_rigid(self, buffer_updates):
        if self.sim.rigid_solver.is_active():
            for rigid_entity in self.sim.rigid_solver.entities:
                if rigid_entity.surface.vis_mode == "visual":
                    geoms = rigid_entity.vgeoms
                    geoms_T = self.sim.rigid_solver._vgeoms_render_T
                else:
                    geoms = rigid_entity.geoms
                    geoms_T = self.sim.rigid_solver._geoms_render_T

                for geom in geoms:
                    geom_T = geoms_T[geom.idx][self.rendered_envs_idx]
                    node = self.rigid_nodes[geom.uid]
                    node.mesh._bounds = None
                    for primitive in node.mesh.primitives:
                        primitive.poses = geom_T
                    buffer_updates[self._scene.get_buffer_id(node, "model")] = geom_T.transpose((0, 2, 1))
                    if isinstance(rigid_entity._morph, gs.morphs.Plane):
                        self.set_reflection_mat(geom_T)

    def update_contact(self, buffer_updates):
        if self.sim.rigid_solver.is_active() and any(link.visualize_contact for link in self.sim.rigid_solver.links):
            # Extract all contact information at once
            contacts_info = self.sim.rigid_solver.collider.get_contacts(as_tensor=False, to_torch=False)

            # Only visualize contact for the first scene
            batch_idx = 0
            if self.sim.rigid_solver.n_envs > 0:
                contacts_info = {key: value[batch_idx] for key, value in contacts_info.items()}

            # Early return if no contact
            n_contacts = len(contacts_info["geom_a"])
            if n_contacts == 0:
                return

            geoms_aabb = self.sim.rigid_solver.geoms_init_AABB.to_numpy()
            ga_aabb = geoms_aabb[contacts_info["geom_a"]]
            gb_aabb = geoms_aabb[contacts_info["geom_b"]]
            ga_aabb_size = np.linalg.norm(ga_aabb[:, -1] - ga_aabb[:, 0], axis=1)
            gb_aabb_size = np.linalg.norm(gb_aabb[:, -1] - gb_aabb[:, 0], axis=1)
            normal_scale = np.minimum(ga_aabb_size, gb_aabb_size)

            contact_pos = contacts_info["position"] + self.scene.envs_offset[batch_idx]
            contact_normal_scaled = contacts_info["normal"] * normal_scale[:, None]
            contact_force = contacts_info["force"]

            for i_c in range(n_contacts):
                for link_idx, sign in (
                    (contacts_info["link_a"][i_c], -1),
                    (contacts_info["link_b"][i_c], 1),
                ):
                    if self.sim.rigid_solver.links[link_idx].visualize_contact:
                        self.draw_contact_arrow(
                            pos=contact_pos[i_c],
                            force=sign * contact_force[i_c],
                        )
                        self.draw_debug_arrow(
                            pos=contact_pos[i_c],
                            vec=-sign * contact_normal_scaled[i_c],
                            color=(0.9, 0.0, 0.8, 1.0),
                            persistent=False,
                        )

    def on_avatar(self):
        if self.sim.avatar_solver.is_active():
            # TODO: support dynamic switching in GUI later
            for avatar_entity in self.sim.avatar_solver.entities:
                if avatar_entity.surface.vis_mode == "visual":
                    geoms = avatar_entity.vgeoms
                    geoms_T = self.sim.avatar_solver._vgeoms_render_T
                else:
                    geoms = avatar_entity.geoms
                    geoms_T = self.sim.avatar_solver._geoms_render_T

                for geom in geoms:
                    if "sdf" in avatar_entity.surface.vis_mode:
                        mesh = geom.get_sdf_trimesh()
                    else:
                        mesh = geom.get_trimesh()
                    geom_T = geoms_T[geom.idx]
                    self.add_rigid_node(
                        geom,
                        pyrender.Mesh.from_trimesh(
                            mesh=mesh,
                            poses=geom_T,
                            smooth=geom.surface.smooth if "collision" not in avatar_entity.surface.vis_mode else False,
                            double_sided=(
                                geom.surface.double_sided
                                if "collision" not in avatar_entity.surface.vis_mode
                                else False
                            ),
                        ),
                    )

    def update_avatar(self, buffer_updates):
        if self.sim.avatar_solver.is_active():
            for avatar_entity in self.sim.avatar_solver.entities:
                if avatar_entity.surface.vis_mode == "visual":
                    geoms = avatar_entity.vgeoms
                    geoms_T = self.sim.avatar_solver._vgeoms_render_T
                else:
                    geoms = avatar_entity.geoms
                    geoms_T = self.sim.avatar_solver._geoms_render_T

                for geom in geoms:
                    geom_T = geoms_T[geom.idx]
                    node = self._scene.get_buffer_id(self.rigid_nodes[geom.uid], "model")
                    node.mesh._bounds = None
                    for primitive in node.mesh.primitives:
                        primitive.poses = geom_T
                    buffer_updates[node] = geom_T.transpose((0, 2, 1))

    def on_mpm(self):
        if self.sim.mpm_solver.is_active():
            for mpm_entity in self.sim.mpm_solver.entities:
                if mpm_entity.surface.vis_mode == "recon":
                    self.add_dynamic_node(mpm_entity, None)

                elif mpm_entity.surface.vis_mode == "particle":
                    mesh = mu.create_sphere(
                        self.sim.mpm_solver.particle_radius * self.particle_size_scale, subdivisions=1
                    )
                    mesh.visual = mu.surface_uvs_to_trimesh_visual(mpm_entity.surface, n_verts=len(mesh.vertices))

                    tfs = np.tile(np.eye(4), (mpm_entity.n_particles, 1, 1))
                    tfs[:, :3, 3] = mpm_entity.init_particles
                    self.add_static_node(mpm_entity, pyrender.Mesh.from_trimesh(mesh, smooth=True, poses=tfs))

                elif mpm_entity.surface.vis_mode == "visual":
                    # self.add_static_node(mpm_entity, pyrender.Mesh.from_trimesh(mesh, smooth=mpm_entity.surface.smooth))
                    self.add_dynamic_node(
                        mpm_entity,
                        pyrender.Mesh.from_trimesh(mpm_entity.vmesh.trimesh, smooth=mpm_entity.surface.smooth),
                    )

            # boundary
            if self.visualize_mpm_boundary:
                self.add_node(
                    pyrender.Mesh.from_trimesh(
                        mu.create_box(
                            bounds=np.array(
                                [self.sim.mpm_solver.boundary.lower, self.sim.mpm_solver.boundary.upper],
                                dtype=np.float32,
                            ),
                            wireframe=True,
                            color=(1.0, 1.0, 0.0, 1.0),
                        ),
                        smooth=True,
                    )
                )

    def update_mpm(self, buffer_updates):
        if self.sim.mpm_solver.is_active():
            idx = self.rendered_envs_idx[0]
            particles_all = self.sim.mpm_solver.particles_render.pos.to_numpy()[:, idx]
            active_all = self.sim.mpm_solver.particles_render.active.to_numpy(dtype=np.bool_)[:, idx]
            vverts_all = self.sim.mpm_solver.vverts_render.pos.to_numpy()[:, idx, :]

            for mpm_entity in self.sim.mpm_solver.entities:
                if mpm_entity.surface.vis_mode == "recon":
                    mesh = pu.particles_to_mesh(
                        positions=particles_all[mpm_entity.particle_start : mpm_entity.particle_end][
                            active_all[mpm_entity.particle_start : mpm_entity.particle_end]
                        ],
                        radius=self.sim.mpm_solver.particle_radius,
                        backend=mpm_entity.surface.recon_backend,
                    )
                    mesh.visual = mu.surface_uvs_to_trimesh_visual(mpm_entity.surface, n_verts=len(mesh.vertices))
                    self.add_dynamic_node(mpm_entity, pyrender.Mesh.from_trimesh(mesh, smooth=True))

                elif mpm_entity.surface.vis_mode == "particle":
                    tfs = np.tile(np.eye(4), (mpm_entity.n_particles, 1, 1))
                    tfs[:, :3, 3] = particles_all[mpm_entity.particle_start : mpm_entity.particle_end]

                    node = self._scene.get_buffer_id(self.static_nodes[mpm_entity.uid], "model")
                    buffer_updates[node] = tfs.transpose((0, 2, 1))

                elif mpm_entity.surface.vis_mode == "visual":
                    mpm_entity._vmesh.verts = vverts_all[mpm_entity.vvert_start : mpm_entity.vvert_end]
                    self.add_dynamic_node(
                        mpm_entity,
                        pyrender.Mesh.from_trimesh(mpm_entity.vmesh.trimesh, smooth=mpm_entity.surface.smooth),
                    )

    def on_sph(self):
        if self.sim.sph_solver.is_active():
            for sph_entity in self.sim.sph_solver.entities:
                if sph_entity.surface.vis_mode == "recon":
                    self.add_dynamic_node(sph_entity, None)

                elif sph_entity.surface.vis_mode == "particle":
                    mesh = mu.create_sphere(
                        self.sim.sph_solver.particle_radius * self.particle_size_scale, subdivisions=1
                    )
                    mesh.visual = mu.surface_uvs_to_trimesh_visual(sph_entity.surface, n_verts=len(mesh.vertices))

                    tfs = np.tile(np.eye(4), (sph_entity.n_particles, 1, 1))
                    tfs[:, :3, 3] = sph_entity.init_particles
                    self.add_static_node(sph_entity, pyrender.Mesh.from_trimesh(mesh, smooth=True, poses=tfs))

            # boundary
            if self.visualize_sph_boundary:
                self.add_node(
                    pyrender.Mesh.from_trimesh(
                        mu.create_box(
                            bounds=np.array(
                                [self.sim.sph_solver.boundary.lower, self.sim.sph_solver.boundary.upper],
                                dtype=np.float32,
                            ),
                            wireframe=True,
                            color=(0.0, 1.0, 1.0, 1.0),
                        ),
                        smooth=True,
                    )
                )

    def update_sph(self, buffer_updates):
        if self.sim.sph_solver.is_active():
            idx = self.rendered_envs_idx[0]
            particles_all = self.sim.sph_solver.particles_render.pos.to_numpy()[:, idx]
            active_all = self.sim.sph_solver.particles_render.active.to_numpy(dtype=np.bool_)[:, idx]

            for sph_entity in self.sim.sph_solver.entities:
                if sph_entity.surface.vis_mode == "recon":
                    mesh = pu.particles_to_mesh(
                        positions=particles_all[sph_entity.particle_start : sph_entity.particle_end][
                            active_all[sph_entity.particle_start : sph_entity.particle_end]
                        ],
                        radius=self.sim.sph_solver.particle_radius,
                        backend=sph_entity.surface.recon_backend,
                    )
                    mesh.visual = mu.surface_uvs_to_trimesh_visual(sph_entity.surface, n_verts=len(mesh.vertices))
                    self.add_dynamic_node(sph_entity, pyrender.Mesh.from_trimesh(mesh, smooth=True))

                elif sph_entity.surface.vis_mode == "particle":
                    tfs = np.tile(np.eye(4), (sph_entity.n_particles, 1, 1))
                    tfs[:, :3, 3] = particles_all[sph_entity.particle_start : sph_entity.particle_end]

                    node = self._scene.get_buffer_id(self.static_nodes[sph_entity.uid], "model")
                    buffer_updates[node] = tfs.transpose((0, 2, 1))

    def on_pbd(self):
        if self.sim.pbd_solver.is_active():
            for pbd_entity in self.sim.pbd_solver.entities:
                if pbd_entity.surface.vis_mode == "recon":
                    self.add_dynamic_node(pbd_entity, None)

                elif pbd_entity.surface.vis_mode == "particle":
                    if self.render_particle_as == "sphere":
                        mesh = mu.create_sphere(
                            self.sim.pbd_solver.particle_radius * self.particle_size_scale, subdivisions=1
                        )
                        mesh.visual = mu.surface_uvs_to_trimesh_visual(pbd_entity.surface, n_verts=len(mesh.vertices))
                        tfs = np.tile(np.eye(4), (pbd_entity.n_particles, 1, 1))
                        tfs[:, :3, 3] = pbd_entity.init_particles
                        self.add_static_node(pbd_entity, pyrender.Mesh.from_trimesh(mesh, smooth=True, poses=tfs))

                    elif self.render_particle_as == "tet":
                        mesh = mu.create_tets_mesh(
                            pbd_entity.n_particles, self.sim.pbd_solver.particle_radius * self.particle_size_scale
                        )
                        mesh.visual = mu.surface_uvs_to_trimesh_visual(pbd_entity.surface, n_verts=len(mesh.vertices))
                        pbd_entity._tets_mesh = mesh
                        self.add_static_node(pbd_entity, pyrender.Mesh.from_trimesh(mesh, smooth=False))

                elif pbd_entity.surface.vis_mode == "visual":
                    self.add_static_node(
                        pbd_entity,
                        pyrender.Mesh.from_trimesh(
                            pbd_entity.vmesh.trimesh,
                            smooth=pbd_entity.surface.smooth,
                            double_sided=pbd_entity._surface.double_sided,
                        ),
                    )

            # boundary
            if self.visualize_pbd_boundary:
                self.add_node(
                    pyrender.Mesh.from_trimesh(
                        mu.create_box(
                            bounds=np.array(
                                [self.sim.pbd_solver.boundary.lower, self.sim.pbd_solver.boundary.upper],
                                dtype=np.float32,
                            ),
                            wireframe=True,
                            color=(0.0, 1.0, 1.0, 1.0),
                        ),
                        smooth=True,
                    )
                )

    def update_pbd(self, buffer_updates):
        if self.sim.pbd_solver.is_active():
            idx = self.rendered_envs_idx[0]
            particles_all = self.sim.pbd_solver.particles_render.pos.to_numpy()[:, idx]
            particles_vel_all = self.sim.pbd_solver.particles_render.vel.to_numpy()[:, idx]
            active_all = self.sim.pbd_solver.particles_render.active.to_numpy(dtype=np.bool_)[:, idx]
            vverts_all = self.sim.pbd_solver.vverts_render.pos.to_numpy()[:, idx]

            for pbd_entity in self.sim.pbd_solver.entities:
                if pbd_entity.surface.vis_mode == "recon":
                    mesh = pu.particles_to_mesh(
                        positions=particles_all[pbd_entity.particle_start : pbd_entity.particle_end][
                            active_all[pbd_entity.particle_start : pbd_entity.particle_end]
                        ],
                        radius=self.sim.mpm_solver.particle_radius,
                        backend=pbd_entity.surface.recon_backend,
                    )
                    mesh.visual = mu.surface_uvs_to_trimesh_visual(pbd_entity.surface, n_verts=len(mesh.vertices))
                    self.add_dynamic_node(pbd_entity, pyrender.Mesh.from_trimesh(mesh, smooth=True))

                elif pbd_entity.surface.vis_mode == "particle":
                    if self.render_particle_as == "sphere":
                        tfs = np.tile(np.eye(4), (pbd_entity.n_particles, 1, 1))
                        tfs[:, :3, 3] = particles_all[pbd_entity.particle_start : pbd_entity.particle_end]

                        node = self._scene.get_buffer_id(self.static_nodes[pbd_entity.uid], "model")
                        buffer_updates[node] = tfs.transpose((0, 2, 1))

                    elif self.render_particle_as == "tet":
                        new_verts = mu.transform_tets_mesh_verts(
                            pbd_entity._tets_mesh.vertices,
                            positions=particles_all[pbd_entity.particle_start : pbd_entity.particle_end],
                            zs=particles_vel_all[pbd_entity.particle_start : pbd_entity.particle_end],
                        )
                        node = self.static_nodes[pbd_entity.uid]
                        update_data = self._scene.reorder_vertices(node, new_verts.astype(np.float32))
                        buffer_updates[self._scene.get_buffer_id(node, "pos")] = update_data
                        normal_data = self.jit.update_normal(node, update_data)
                        if normal_data is not None:
                            buffer_updates[self._scene.get_buffer_id(node, "normal")] = normal_data

                elif pbd_entity.surface.vis_mode == "visual":
                    vverts = vverts_all[pbd_entity.vvert_start : pbd_entity.vvert_end]
                    node = self.static_nodes[pbd_entity.uid]
                    update_data = self._scene.reorder_vertices(node, vverts.astype(np.float32))
                    buffer_updates[self._scene.get_buffer_id(node, "pos")] = update_data
                    normal_data = self.jit.update_normal(node, update_data)
                    if normal_data is not None:
                        buffer_updates[self._scene.get_buffer_id(node, "normal")] = normal_data

    def on_fem(self):
        if self.sim.fem_solver.is_active():
            vertices_all, triangles_all = self.sim.fem_solver.get_state_render(self.sim.cur_substep_local)
            vertices_all = vertices_all.to_numpy(dtype=gs.np_float)[:, self.rendered_envs_idx[0]]
            triangles_all = triangles_all.to_numpy(dtype=gs.np_int).reshape((-1, 3))

            for fem_entity in self.sim.fem_solver.entities:
                if fem_entity.surface.vis_mode == "visual":
                    vertices = vertices_all[fem_entity.v_start : fem_entity.v_start + fem_entity.n_vertices]
                    triangles = (
                        triangles_all[fem_entity.s_start : (fem_entity.s_start + fem_entity.n_surfaces)]
                        - fem_entity.v_start
                    )

                    # Select only vertices used in surface triangles, then reindex triangles against the new vertex list
                    surf_idx, inv = np.unique(triangles.flat, return_inverse=True)
                    triangles = inv.reshape(triangles.shape)
                    vertices = vertices[surf_idx]

                    mesh = trimesh.Trimesh(vertices, triangles, process=False)
                    mesh.visual = mu.surface_uvs_to_trimesh_visual(
                        fem_entity.surface, n_verts=fem_entity.n_surface_vertices
                    )
                    self.add_static_node(
                        fem_entity, pyrender.Mesh.from_trimesh(mesh, double_sided=fem_entity.surface.double_sided)
                    )

    def update_fem(self, buffer_updates):
        if self.sim.fem_solver.is_active():
            vertices_all, triangles_all = self.sim.fem_solver.get_state_render(self.sim.cur_substep_local)
            vertices_all = vertices_all.to_numpy(dtype=gs.np_float)[:, self.rendered_envs_idx[0]]
            triangles_all = triangles_all.to_numpy(dtype=gs.np_int).reshape((-1, 3))

            for fem_entity in self.sim.fem_solver.entities:
                if fem_entity.surface.vis_mode == "visual":
                    vertices = vertices_all[fem_entity.v_start : fem_entity.v_start + fem_entity.n_vertices]
                    triangles = (
                        triangles_all[fem_entity.s_start : (fem_entity.s_start + fem_entity.n_surfaces)]
                        - fem_entity.v_start
                    )
                    node = self.static_nodes[fem_entity.uid]
                    update_data = self._scene.reorder_vertices(node, vertices)
                    buffer_updates[self._scene.get_buffer_id(node, "pos")] = update_data

    def on_lights(self):
        for light in self.lights:
            self.add_light(light)

    def draw_debug_line(self, start, end, radius=0.002, color=(1.0, 0.0, 0.0, 1.0)):
        mesh = mu.create_line(
            tensor_to_array(start, dtype=np.float32), tensor_to_array(end, dtype=np.float32), radius, color
        )
        node = pyrender.Mesh.from_trimesh(mesh, name=f"debug_line_{gs.UID()}")
        self.add_external_node(node)
        return node

    def draw_debug_arrow(self, pos, vec=(0.0, 0.0, 1.0), radius=0.006, color=(1.0, 0.0, 0.0, 0.5), persistent=True):
        length = np.linalg.norm(vec)
        if length > 0:
            mesh = mu.create_arrow(length=length, radius=radius, body_color=color, head_color=color)

            pose = np.zeros((1, 4, 4), dtype=np.float32)
            pose[0, 3, 3] = 1.0
            pose[0, :3, 3] = tensor_to_array(pos)
            gu.z_up_to_R(tensor_to_array(vec).astype(np.float32), out=pose[0, :3, :3])

            node = pyrender.Mesh.from_trimesh(mesh, name=f"debug_arrow_{gs.UID()}", poses=pose)
            if persistent:
                self.add_external_node(node)
            else:
                self.add_dynamic_node(None, node)
            return node

    def draw_debug_frame(self, T, axis_length=1.0, origin_size=0.015, axis_radius=0.01):
        node = pyrender.Mesh.from_trimesh(
            trimesh.creation.axis(
                origin_size=origin_size,
                axis_radius=axis_radius,
                axis_length=axis_length,
            ),
            name=f"debug_frame_{gs.UID()}",
        )
        self.add_external_node(node, pose=T)
        return node

    def draw_debug_frames(self, poses, axis_length=1.0, origin_size=0.015, axis_radius=0.01):
        node = pyrender.Mesh.from_trimesh(
            trimesh.creation.axis(
                origin_size=origin_size,
                axis_radius=axis_radius,
                axis_length=axis_length,
            ),
            name=f"debug_frame_{gs.UID()}",
            poses=poses,
        )
        self.add_external_node(node)
        return node

    def draw_debug_mesh(self, mesh, pos=np.zeros(3), T=None):
        if T is None:
            T = gu.trans_to_T(tensor_to_array(pos))
        node = pyrender.Mesh.from_trimesh(mesh, name=f"debug_mesh_{gs.UID()}")
        self.add_external_node(node, pose=T)
        return node

    def draw_contact_arrow(self, pos, radius=0.005, force=(0, 0, 1), color=(0.0, 0.9, 0.8, 1.0)):
        self.draw_debug_arrow(
            pos, tensor_to_array(force) * self.contact_force_scale, radius, color=color, persistent=False
        )

    def draw_debug_sphere(self, pos, radius=0.01, color=(1.0, 0.0, 0.0, 0.5), persistent=True):
        mesh = mu.create_sphere(radius=radius, color=color)
        pose = gu.trans_to_T(tensor_to_array(pos))
        node = pyrender.Mesh.from_trimesh(mesh, name=f"debug_sphere_{gs.UID()}", smooth=True, poses=pose[None])
        if persistent:
            self.add_external_node(node)
        else:
            self.add_dynamic_node(None, node)
        return node

    def draw_debug_spheres(self, poss, radius=0.01, color=(1.0, 0.0, 0.0, 0.5), persistent=True):
        mesh = mu.create_sphere(radius=radius, color=color)
        poses = gu.trans_to_T(tensor_to_array(poss))
        node = pyrender.Mesh.from_trimesh(mesh, name=f"debug_spheres_{gs.UID()}", smooth=True, poses=poses)
        if persistent:
            self.add_external_node(node)
        else:
            self.add_dynamic_node(None, node)
        return node

    def draw_debug_box(self, bounds, color=(1.0, 0.0, 0.0, 1.0), wireframe=True, wireframe_radius=0.002):
        bounds = tensor_to_array(bounds)
        mesh = mu.create_box(
            bounds=bounds,
            wireframe=wireframe,
            wireframe_radius=wireframe_radius,
            color=color,
        )
        node = pyrender.Mesh.from_trimesh(mesh, name=f"debug_box_{gs.UID()}")
        self.add_external_node(node)
        return node

    def draw_debug_points(self, poss, colors=(1.0, 0.0, 0.0, 0.5)):
        poss = tensor_to_array(poss)
        colors = tensor_to_array(colors)
        if len(colors.shape) == 1:
            colors = np.tile(colors, [len(poss), 1])
        elif len(colors.shape) == 2:
            assert colors.shape[0] == len(poss)

        node = pyrender.Mesh.from_points(poss, name=f"debug_box_{gs.UID()}", colors=colors)
        self.add_external_node(node)
        return node

    def clear_debug_object(self, object):
        self.clear_external_node(object)

    def clear_debug_objects(self):
        self.clear_external_nodes()

    def update(self):
        # Early return if already updated previously
        if self._t >= self.scene._t:
            return

        self._t = self.scene._t

        # clear up all dynamic nodes
        self.clear_dynamic_nodes()

        # update variables not used in simulation
        self.visualizer.update_visual_states()

        # Reset scene bounds to trigger recomputation. They are involved in shadow map
        self._scene._bounds = None

        self.buffer.clear()
        self.update_link_frame(self.buffer)
        self.update_tool(self.buffer)
        self.update_rigid(self.buffer)
        self.update_contact(self.buffer)
        self.update_avatar(self.buffer)
        self.update_mpm(self.buffer)
        self.update_sph(self.buffer)
        self.update_pbd(self.buffer)
        self.update_fem(self.buffer)

    def add_light(self, light):
        # light direction is light pose's -z frame
        if light["type"] == "directional":
            pose = np.eye(4, dtype=np.float32)
            gu.z_up_to_R(-np.asarray(light["dir"], dtype=np.float32), out=pose[:3, :3])
            self.add_node(pyrender.DirectionalLight(color=light["color"], intensity=light["intensity"]), pose=pose)
        elif light["type"] == "point":
            pose = gu.trans_to_T(np.asarray(light["pos"], dtype=np.float32))
            self.add_node(pyrender.PointLight(color=light["color"], intensity=light["intensity"]), pose=pose)
        else:
            gs.raise_exception(f"Unsupported light type: {light['type']}")

    def create_node_seg(self, seg_key, seg_node):
        seg_idxc = self.seg_key_to_idxc(seg_key)
        if seg_node:
            self.seg_node_map[seg_node] = self.seg_idxc_to_idxc_rgb(seg_idxc)

    def remove_node_seg(self, seg_node):
        self.seg_node_map.pop(seg_node, None)

    def generate_seg_vars(self):
        # seg_key: same as entity/link/geom's idx
        # seg_idxc: segmentation index of objects
        # seg_idxc_rgb: colorized seg_idxc internally used by renderer
        num_keys = len(self.seg_key_map)
        rng = np.random.default_rng(seed=42)
        self.seg_idxc_to_color = rng.integers(0, 255, size=(num_keys, 3), dtype=np.uint8)
        self.seg_idxc_to_color[0] = 0  # background uses black

    def seg_key_to_idxc(self, seg_key):
        seg_idxc = self.seg_key_map.setdefault(seg_key, len(self.seg_key_map))
        self.seg_idxc_map[seg_idxc] = seg_key
        return seg_idxc

    def seg_idxc_to_idxc_rgb(self, seg_idxc):
        seg_idxc_rgb = np.array(
            [
                (seg_idxc >> 16) & 0xFF,
                (seg_idxc >> 8) & 0xFF,
                seg_idxc & 0xFF,
            ],
            dtype=np.int32,
        )
        return seg_idxc_rgb

    def seg_idxc_to_key(self, seg_idxc):
        return self.seg_idxc_map[seg_idxc]

    def seg_idxc_rgb_arr_to_idxc_arr(self, seg_idxc_rgb_arr):
        # Combine the RGB components into a single integer
        seg_idxc_rgb_arr = seg_idxc_rgb_arr.astype(np.int64, copy=False)
        return seg_idxc_rgb_arr[..., 0] * (256 * 256) + seg_idxc_rgb_arr[..., 1] * 256 + seg_idxc_rgb_arr[..., 2]

    def colorize_seg_idxc_arr(self, seg_idxc_arr):
        return self.seg_idxc_to_color[seg_idxc_arr]

    @property
    def cameras(self):
        return self.visualizer.cameras
